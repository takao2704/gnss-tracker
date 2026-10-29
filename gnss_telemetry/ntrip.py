import base64
import logging
import socket
import time


def request(host,mount,user,password):
    token=base64.b64encode(f'{user}:{password}'.encode()).decode()
    return (f'GET /{mount} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: NTRIP gnss-comparison/1.0\r\nAuthorization: Basic {token}\r\nNtrip-Version: Ntrip/1.0\r\n\r\n').encode()


def read_header(sock):
    data=b''
    while len(data)<16384:
        chunk=sock.recv(4096)
        if not chunk:raise ConnectionError('Caster closed during header')
        data+=chunk
        if b'\r\n' not in data:continue
        line=data.split(b'\r\n',1)[0]
        parts=line.split()
        if len(parts)<2 or parts[1]!=b'200':raise ConnectionError('Caster rejected request')
        # NTRIP v1 ICY often has no HTTP header block.
        if line.startswith(b'ICY '):return data.split(b'\r\n',1)[1].removeprefix(b'\r\n')
        if b'\r\n\r\n' in data:
            headers,body=data.split(b'\r\n\r\n',1)
            if b'transfer-encoding: chunked' in headers.lower():raise ConnectionError('Chunked caster response unsupported')
            return body
    raise ConnectionError('Caster header too large')

class Ntrip:
    def __init__(self,args,ser,measurements,stop,write_lock):
        self.args=args;self.ser=ser;self.measurements=measurements;self.stop=stop;self.lock=write_lock
        self.state={'connected':False,'bytes':0,'reconnects':0,'last_rtcm_monotonic':None}

    def run(self):
        delay=5
        while not self.stop.is_set():
            try:
                with socket.create_connection((self.args.ntrip_host,self.args.ntrip_port),timeout=10) as sock:
                    sock.settimeout(10);sock.sendall(request(self.args.ntrip_host,self.args.ntrip_mount,self.args.ntrip_user,self.args.ntrip_pass))
                    pending=read_header(sock);sock.settimeout(1)
                    logging.info('NTRIP connected');self.state['connected']=True
                    received=time.monotonic();last_gga=0;last_log=0
                    while not self.stop.is_set():
                        now=time.monotonic()
                        gga=self.measurements.last_gga
                        if gga and now-self.measurements.last_gga_seen<15 and now-last_gga>=1:
                            sock.sendall((gga+'\r\n').encode());last_gga=now
                        if pending:data=pending;pending=b''
                        else:
                            try:data=sock.recv(4096)
                            except socket.timeout:
                                if now-received>30:raise ConnectionError('No correction data for 30 seconds')
                                continue
                        if not data:raise ConnectionError('Caster disconnected')
                        with self.lock:
                            if self.ser.write(data)!=len(data):raise OSError('Incomplete RTCM serial write')
                        received=time.monotonic();delay=5
                        self.state['bytes']+=len(data);self.state['last_rtcm_monotonic']=received
                        if now-last_log>10:logging.info('NTRIP RTCM bytes=%s',self.state['bytes']);last_log=now
            except Exception:
                self.state['connected']=False;self.state['reconnects']+=1
                if not self.stop.is_set():logging.warning('NTRIP disconnected; reconnect in %ss',delay)
                self.stop.wait(delay);delay=min(60,delay*2)
        self.state['connected']=False
