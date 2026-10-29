"""Durable bounded outbox. Stable payloads; at-least-once delivery."""
import email.utils
import json
import logging
import random
import sqlite3
import threading
import time
from pathlib import Path
import requests

class Outbox:
    def __init__(self,path,max_records=50000,max_bytes=100*1024*1024):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.max_records=max_records;self.max_bytes=max_bytes
        self.db=sqlite3.connect(self.path,timeout=15,check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS queue(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,body TEXT NOT NULL,
        size INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        due REAL NOT NULL DEFAULT 0,error TEXT);
        CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS queue_state ON queue(state,seq);''')
        self.path.chmod(0o600)
        self._last_checkpoint=time.monotonic()

    def _count(self,key):
        self.db.execute('INSERT INTO stats VALUES (?,1) ON CONFLICT(key) DO UPDATE SET value=value+1',(key,))

    def enqueue(self,payload):
        body=json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False);size=len(body.encode())
        if size>self.max_bytes:raise ValueError('Payload exceeds outbox limit')
        with self.lock,self.db:
            if self.db.execute('SELECT 1 FROM queue WHERE event_id=?',(payload['event_id'],)).fetchone():return False
            count,total=self.db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM queue').fetchone()
            while count>=self.max_records or total+size>self.max_bytes:
                seq,removed=self.db.execute('SELECT seq,size FROM queue ORDER BY seq LIMIT 1').fetchone()
                self.db.execute('DELETE FROM queue WHERE seq=?',(seq,));self._count('overflow_dropped')
                logging.error('OUTBOX limit reached; oldest record dropped')
                count-=1;total-=removed
            self.db.execute('INSERT INTO queue(event_id,body,size) VALUES (?,?,?)',(payload['event_id'],body,size))
            self._count('enqueued')
        return True

    def head(self,now=None):
        now=time.time() if now is None else now
        with self.lock:
            row=self.db.execute("SELECT seq,event_id,body,attempts,due FROM queue WHERE state='pending' ORDER BY seq LIMIT 1").fetchone()
        return row if row and row[4]<=now else None

    def ack(self,seq):
        with self.lock,self.db:
            self.db.execute('DELETE FROM queue WHERE seq=?',(seq,));self._count('acked')

    def retry(self,seq,attempts,delay,error,now=None):
        with self.lock,self.db:
            self.db.execute('UPDATE queue SET attempts=?,due=?,error=? WHERE seq=?',(attempts,(time.time() if now is None else now)+delay,error,seq));self._count('retries')

    def quarantine(self,seq,error):
        with self.lock,self.db:
            self.db.execute("UPDATE queue SET state='quarantine',error=? WHERE seq=?",(error,seq));self._count('quarantined')

    def status(self):
        with self.lock:
            states=dict(self.db.execute('SELECT state,COUNT(*) FROM queue GROUP BY state'))
            stats=dict(self.db.execute('SELECT key,value FROM stats'))
            return {**stats,'pending':states.get('pending',0),'quarantine':states.get('quarantine',0)}

    def checkpoint(self):
        with self.lock:self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def close(self):
        with self.lock:self.db.close()

class Sender:
    def __init__(self,outbox,endpoint,stop,session=None):
        self.outbox=outbox;self.endpoint=endpoint;self.stop=stop
        self.session=session or requests.Session()
        self.session.trust_env=False  # Never send GPS data through a desktop proxy.

    def once(self,now=None):
        row=self.outbox.head(now)
        if not row:return False
        seq,event,body,attempts,_=row
        retry_after=None
        try:
            response=self.session.post(self.endpoint,data=body.encode(),headers={'Content-Type':'application/json'},timeout=(3,5),allow_redirects=False)
            status=response.status_code
            retry_after=response.headers.get('Retry-After');response.close()
            if 200<=status<300:
                self.outbox.ack(seq);logging.info('DELIVERED event=%s HTTP=%s pending=%s',event[:12],status,self.outbox.status()['pending']);return True
            if status not in (401,403,408,425,429) and status<500:
                self.outbox.quarantine(seq,f'HTTP {status}');logging.error('QUARANTINED event=%s HTTP=%s',event[:12],status);return True
            error=f'HTTP {status}'
        except requests.RequestException:
            error='transport_error';status=None
        delay=min(60,5*(2**min(attempts,4)))+random.uniform(0,1)
        if status in (401,403):delay=60
        if retry_after:
            try:delay=max(delay,float(retry_after))
            except ValueError:
                try:delay=max(delay,email.utils.parsedate_to_datetime(retry_after).timestamp()-(time.time() if now is None else now))
                except (TypeError,ValueError,OverflowError):pass
        delay=min(max(delay,1),3600)
        self.outbox.retry(seq,attempts+1,delay,error,now)
        logging.warning('RETRY event=%s reason=%s delay=%.1fs pending=%s',event[:12],error,delay,self.outbox.status()['pending'])
        return False

    def run(self):
        try:
            while not self.stop.is_set():
                if not self.once():self.stop.wait(.5)
                else:self.stop.wait(.1)  # Catch up without flooding the endpoint.
                if time.monotonic()-self.outbox._last_checkpoint>60:
                    self.outbox.checkpoint();self.outbox._last_checkpoint=time.monotonic()
        except Exception:
            logging.exception('Delivery worker failed; restarting service with durable queue retained')
            self.stop.set()
        finally:self.session.close()
