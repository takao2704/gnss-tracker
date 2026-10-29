import argparse
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
import serial
from .measurement import Measurements,utc
from .delivery import Outbox,Sender
from .ntrip import Ntrip
from . import __version__


def arguments(receiver):
    p=argparse.ArgumentParser(description='GNSS UTC-aligned comparison recorder with durable delivery')
    p.add_argument('--serial-port','--port',dest='port',default=os.environ.get('SERIAL_PORT','/dev/ttyACM0'))
    p.add_argument('--baud','--baudrate',dest='baud',type=int,default=int(os.environ.get('SERIAL_BAUD','9600' if receiver=='CLAS' else '115200')))
    p.add_argument('--endpoint',default=os.environ.get('UNIFIED_ENDPOINT','http://harvest.soracom.io'))
    p.add_argument('--run',default=os.environ.get('GNSS_RUN_ID',''))
    p.add_argument('--interval',type=int,default=int(os.environ.get('GNSS_INTERVAL','5')))
    p.add_argument('--spool-dir',default=os.environ.get('GNSS_SPOOL_DIR',str(Path.home()/'.local/state/gnss-comparison'/receiver.lower())))
    p.add_argument('--max-records',type=int,default=50000);p.add_argument('--max-bytes',type=int,default=100*1024*1024)
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--no-ntrip',action='store_true')
    p.add_argument('--ntrip-host',default=os.environ.get('NTRIP_HOST','qrtksa1.quectel.com'))
    p.add_argument('--ntrip-port',type=int,default=int(os.environ.get('NTRIP_PORT','2101')))
    p.add_argument('--ntrip-mount',default=os.environ.get('NTRIP_MOUNT','AUTO'))
    p.add_argument('--ntrip-user',default=os.environ.get('NTRIP_USER',''))
    p.add_argument('--ntrip-pass',default=os.environ.get('NTRIP_PASS',''))
    p.add_argument('--dr-state',choices=['on','off','unchanged'],default=os.environ.get('QLM29H_DR_STATE','off'))
    args=p.parse_args()
    if not args.run:p.error('--run or GNSS_RUN_ID must identify the same experiment on both receivers')
    if args.interval<1 or args.interval>60 or 60%args.interval:p.error('interval must be an integer divisor of 60')
    if args.max_records<1 or args.max_bytes<1024:p.error('outbox limits must be positive')
    return args


def command(body):
    checksum=0
    for c in body:checksum^=ord(c)
    return f'${body}*{checksum:02X}\r\n'.encode()


def run(receiver):
    args=arguments(receiver);os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    stop=threading.Event();requested_stop=threading.Event()
    def terminate(*_):requested_stop.set();stop.set()
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
    root=Path(args.spool_dir);root.mkdir(parents=True,exist_ok=True)
    lock=(root/'process.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    queue=None if args.dry_run else Outbox(root/'outbox.sqlite3',args.max_records,args.max_bytes)
    def emit(payload):
        payload['dr_configured']=args.dr_state if receiver=='RTK' else 'receiver-defined'
        if args.dry_run:print(json.dumps(payload,ensure_ascii=False),flush=True)
        else:queue.enqueue(payload)
    measurements=Measurements(receiver,args.run,emit,args.interval)
    workers=[];ntrip=None;last_health=0;ser=None
    try:
        ser=serial.Serial(args.port,args.baud,timeout=.5,write_timeout=2,exclusive=True)
        write_lock=threading.Lock()
        if receiver=='RTK' and not args.dry_run:
            with write_lock:
                ser.write(command('PQTMCFGRTK,W,1,1'));time.sleep(.2)
                if args.dr_state!='unchanged':ser.write(command('PQTMCFGDR,W,'+('1' if args.dr_state=='on' else '0')));time.sleep(.2)
        if not args.dry_run:
            sender=Sender(queue,args.endpoint,stop)
            workers.append(threading.Thread(target=sender.run,name='delivery',daemon=True))
            if receiver=='RTK' and not args.no_ntrip:
                if not args.ntrip_user or not args.ntrip_pass:raise ValueError('Ntrip credentials missing')
                ntrip=Ntrip(args,ser,measurements,stop,write_lock)
                workers.append(threading.Thread(target=ntrip.run,name='ntrip',daemon=True))
            for worker in workers:worker.start()
        logging.info('START receiver=%s version=%s interval=%ss run=%s durable=%s DR=%s',receiver,__version__,args.interval,args.run,not args.dry_run,args.dr_state if receiver=='RTK' else 'receiver-defined')
        while not stop.is_set():
            line=ser.readline(8192).decode('ascii',errors='ignore').strip()
            if line:measurements.feed(line)
            measurements.flush()
            now=time.monotonic()
            if now-measurements.last_line>30:raise RuntimeError('No valid RMC/GGA input for 30 seconds')
            if now-last_health>=10:
                status={'receiver':receiver,'run':args.run,'version':__version__,'updated_at':utc(dt.datetime.now(dt.timezone.utc)),
                        'sentences':measurements.sentences,'invalid':measurements.invalid,
                        'outbox':queue.status() if queue else {},'ntrip':ntrip.state if ntrip else None}
                temp=root/'status.tmp';temp.write_text(json.dumps(status));os.replace(temp,root/'status.json')
                logging.info('HEALTH receiver=%s queue=%s invalid=%s',receiver,status['outbox'],measurements.invalid);last_health=now
        return 0 if requested_stop.is_set() else 1
    except Exception as exc:
        logging.error('Recorder stopped: %s; durable outbox retained',type(exc).__name__)
        return 1
    finally:
        stop.set()
        try:measurements.flush(force=True)
        except Exception:logging.error('Final sample could not be saved')
        for worker in workers:worker.join(timeout=12)
        if ser:ser.close()
        if queue and not any(w.is_alive() for w in workers):queue.checkpoint();queue.close()
        lock.close()
