import datetime as dt
import hashlib
import math
import time
import pynmea2

UTC=dt.timezone.utc
FIX={0:'NO_FIX',1:'SPS',2:'DGPS',4:'FIX',5:'FLOAT',6:'DR',7:'MANUAL',8:'SIMULATION'}

def utc(value):
    if value is None or value.tzinfo is None: raise ValueError('GNSS UTC is required')
    return value.astimezone(UTC).isoformat(timespec='milliseconds').replace('+00:00','Z')

def number(value):
    try:
        n=float(value)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None

class Measurements:
    """Pair checksum-valid RMC/GGA epochs, sample on GNSS UTC boundaries.

    Invalid fixes retain their GNSS epoch with null coordinates. A dated RMC
    with status V may date a NO_FIX record, never a valid-position record.
    """
    def __init__(self,receiver,run,emit,interval=5,clock=time.monotonic):
        self.receiver,self.run,self.emit=receiver,run,emit
        self.interval=interval;self.clock=clock
        self.anchor=None;self.anchor_seen=0;self.undated=[];self.epochs={}
        self.last_slot=None;self.last_line=clock();self.invalid=0;self.sentences=0
        self.last_gga='';self.last_gga_seen=0

    def feed(self,line):
        now=self.clock()
        try:
            msg=pynmea2.parse(line,check=True)
            if msg.sentence_type not in ('RMC','GGA'):return
            self.sentences+=1;self.last_line=now
            if msg.sentence_type=='RMC':
                stamp=msg.datetime
                if stamp is None or stamp.tzinfo is None or not 2020<=stamp.year<=2099:raise ValueError('Undated RMC')
                self.anchor=stamp;self.anchor_seen=now
                self._put(stamp,'RMC',msg,now)
                pending=self.undated;self.undated=[]
                for seen,gga in pending:
                    if now-seen<=2:self._gga(gga,now)
            else:
                if not msg.timestamp:raise ValueError('Missing GGA time')
                if self.anchor is None or now-self.anchor_seen>15:
                    self.undated=[(t,m) for t,m in self.undated if now-t<=2][-31:]+[(now,msg)]
                else:self._gga(msg,now)
        except (ValueError,TypeError,AttributeError,pynmea2.ParseError):
            self.invalid+=1
        self.flush()

    def _gga(self,msg,now):
        candidates=[dt.datetime.combine(self.anchor.date()+dt.timedelta(days=d),msg.timestamp,tzinfo=UTC) for d in (-1,0,1)]
        stamp=min(candidates,key=lambda x:abs((x-self.anchor).total_seconds()))
        if abs((stamp-self.anchor).total_seconds())>15:
            self.invalid+=1;return
        if int(msg.gps_qual or 0)>0:
            self.last_gga=str(msg);self.last_gga_seen=now
        self._put(stamp,'GGA',msg,now)

    def _put(self,stamp,kind,msg,now):
        key=utc(stamp)
        epoch=self.epochs.setdefault(key,{'stamp':stamp,'seen':now})
        epoch[kind]=msg
        if 'RMC' in epoch and 'GGA' in epoch:
            self.epochs.pop(key);self._emit(epoch)
        while len(self.epochs)>64:self.epochs.pop(next(iter(self.epochs)));self.invalid+=1

    def flush(self,force=False):
        now=self.clock()
        for key,e in list(self.epochs.items()):
            if force or now-e['seen']>=2:
                self.epochs.pop(key);self._emit(e)

    def _emit(self,e):
        # Choose the first epoch in each UTC bucket. For 1 Hz output both
        # recorders select :00/:05/:10...; initial partial bucket is skipped.
        stamp=e['stamp'];seconds=stamp.timestamp();slot=int(seconds//self.interval)
        if self.last_slot is not None and slot<=self.last_slot:return
        if int(seconds)%self.interval!=0:return
        rmc,gga=e.get('RMC'),e.get('GGA')
        quality=int(gga.gps_qual or 0) if gga else None
        valid=bool(rmc and gga and rmc.status=='A' and quality in (1,2,4,5,6))
        lat=number(gga.latitude) if valid else None;lon=number(gga.longitude) if valid else None
        valid=bool(valid and lat is not None and lon is not None and -90<=lat<=90 and -180<=lon<=180)
        measured=utc(stamp)
        event=hashlib.sha256(f'{self.run}|{self.receiver}|{measured}'.encode()).hexdigest()
        p={'schema_version':1,'source':'gnss_comparison','event_id':event,'run':self.run,
           'receiver':self.receiver,'correction_configured':'network_rtk' if self.receiver=='RTK' else 'qzss_clas',
           'measured_at':measured,'recorded_at':utc(dt.datetime.now(UTC)),
           'time_basis':'GNSS RMC UTC + same-epoch GGA' if rmc and gga else 'GNSS dated partial epoch',
           'interval_sec':self.interval,'position_valid':valid,'lat':lat if valid else None,'lon':lon if valid else None,
           'fix_status':FIX.get(quality,'UNKNOWN') if valid else ('NO_FIX' if rmc and gga else 'INCOMPLETE'),
           'gps_quality':quality,'rmc_status':getattr(rmc,'status',None),
           'rmc_mode':getattr(rmc,'mode_indicator',None),
           'satellites':int(gga.num_sats or 0) if gga else None,'hdop':number(gga.horizontal_dil) if gga else None,
           'altitude_m':number(gga.altitude) if gga else None,'geoid_separation_m':number(gga.geo_sep) if gga else None,
           'correction_age_sec':number(gga.age_gps_data) if gga else None,
           'speed_knots':number(rmc.spd_over_grnd) if rmc else None,
           'course_deg':number(rmc.true_course) if rmc else None,
           'nmea_rmc':str(rmc) if rmc else None,'nmea_gga':str(gga) if gga else None}
        self.emit(p);self.last_slot=slot
