import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
import requests
from gnss_telemetry.measurement import Measurements
from gnss_telemetry.delivery import Outbox,Sender
from gnss_telemetry.ntrip import read_header


def sentence(body):
    checksum=0
    for c in body:checksum^=ord(c)
    return f'${body}*{checksum:02X}'
def rmc(t='000000.00',date='010126',status='A'):
    return sentence(f'GNRMC,{t},{status},3500.0000,N,13900.0000,E,0.0,0.0,{date},,,A')
def gga(t='000000.00',quality=4):
    return sentence(f'GNGGA,{t},3500.0000,N,13900.0000,E,{quality},12,0.7,10.0,M,2.0,M,0.5,0000')
def response(code,headers=None):
    r=Mock(status_code=code,headers=headers or {});return r

class MeasurementsTest(unittest.TestCase):
    def setUp(self):self.items=[];self.clock=[0];self.p=Measurements('CLAS','TEST',self.items.append,5,lambda:self.clock[0])
    def test_midnight_gga_first_and_quality_precedence(self):
        self.p.feed(rmc('235959.00','311225'));self.p.feed(gga());self.p.feed(rmc())
        self.assertEqual(len(self.items),1);p=self.items[0]
        self.assertEqual(p['measured_at'],'2026-01-01T00:00:00.000Z');self.assertEqual(p['fix_status'],'FIX')
        self.assertEqual(p['correction_age_sec'],.5);self.assertEqual(p['geoid_separation_m'],2)
    def test_sampling_common_utc_and_stable_id(self):
        for i in range(12):self.p.feed(rmc(f'1200{i:02}.00'));self.p.feed(gga(f'1200{i:02}.00'))
        self.assertEqual([p['measured_at'][17:19] for p in self.items],['00','05','10'])
        other=[];p=Measurements('CLAS','TEST',other.append);p.feed(rmc('120000.00'));p.feed(gga('120000.00'))
        self.assertEqual(self.items[0]['event_id'],other[0]['event_id'])
    def test_pending_gga_before_any_rmc(self):
        self.p.feed(gga());self.assertEqual(self.items,[]);self.p.feed(rmc());self.assertTrue(self.items[0]['position_valid'])
    def test_bad_checksum_rejected(self):
        self.p.feed(rmc()[:-2]+'00');self.p.feed(gga());self.assertEqual(self.items,[])
    def test_no_fix_record_has_no_coordinates(self):
        self.p.feed(rmc(status='V'));self.p.feed(gga(quality=0));p=self.items[0]
        self.assertFalse(p['position_valid']);self.assertIsNone(p['lat']);self.assertEqual(p['fix_status'],'NO_FIX')
    def test_stale_anchor_rejected(self):
        self.p.feed(rmc('235959.00','311225'));self.clock[0]=20;self.p.feed(gga())
        self.assertEqual(self.items,[])
    def test_partial_epoch_is_not_valid_position(self):
        self.p.feed(rmc());self.clock[0]=3;self.p.flush()
        self.assertEqual(self.items[0]['fix_status'],'INCOMPLETE');self.assertIsNone(self.items[0]['lon'])
    def test_missing_date_and_mismatched_epoch(self):
        self.p.feed(rmc(date=''));self.p.feed(gga());self.assertEqual(self.items,[])
        self.p.feed(rmc('235959.00','311225'));self.p.feed(gga('000100.00'));self.assertEqual(self.items,[])

class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'outbox.db';self.q=Outbox(self.path)
        self.session=Mock();self.sender=Sender(self.q,'http://example.invalid',threading.Event(),self.session)
    def tearDown(self):self.q.close();self.temp.cleanup()
    def put(self,event='TEST'):self.q.enqueue({'event_id':event,'measured_at':'2026-01-01T00:00:00Z'})
    def test_enqueue_dedup_and_restart(self):
        self.put();self.put();self.q.close();self.q=Outbox(self.path)
        self.assertEqual(self.q.status()['pending'],1);self.assertEqual(self.q.head()[1],'TEST')
    def test_timeout_then_restart_retry_same_body(self):
        self.put();self.session.post.side_effect=requests.Timeout()
        self.sender.once(now=100);self.assertEqual(self.q.status()['pending'],1)
        first=self.session.post.call_args.kwargs['data'];self.q.close();self.q=Outbox(self.path)
        self.sender=Sender(self.q,'http://example.invalid',threading.Event(),self.session)
        self.session.post.side_effect=None;self.session.post.return_value=response(201)
        self.assertTrue(self.sender.once(now=200));self.assertEqual(self.q.status()['pending'],0)
        self.assertEqual(first,self.session.post.call_args.kwargs['data'])
    def test_429_retry_after_and_fifo(self):
        self.put('A');self.put('B');self.session.post.return_value=response(429,{'Retry-After':'90'})
        self.sender.once(now=100);self.assertIsNone(self.q.head(now=150));self.assertEqual(self.q.head(now=191)[1],'A')
    def test_permanent_bad_request_quarantines_without_blocking(self):
        self.put('A');self.put('B');self.session.post.return_value=response(400);self.sender.once()
        self.assertEqual(self.q.status()['quarantine'],1);self.assertEqual(self.q.head()[1],'B')
    def test_auth_failure_retained(self):
        self.put();self.session.post.return_value=response(403);self.sender.once(now=100)
        self.assertEqual(self.q.status()['pending'],1);self.assertEqual(self.q.status()['quarantine'],0)
        self.assertIsNone(self.q.head(now=159))
    def test_overflow_is_bounded_and_counted(self):
        self.q.max_records=2
        for x in 'ABC':self.put(x)
        self.assertEqual(self.q.status()['pending'],2);self.assertEqual(self.q.status()['overflow_dropped'],1)
        self.assertEqual(self.q.head()[1],'B')
    def test_500_retry_and_redirect_no_follow(self):
        self.put();self.session.post.return_value=response(500);self.sender.once(now=100)
        self.assertEqual(self.q.status()['retries'],1)
        self.assertFalse(self.session.post.call_args.kwargs['allow_redirects'])
    def test_recovery_can_delete_only_acknowledged_record(self):
        self.put('A');self.put('B');self.session.post.return_value=response(201);self.sender.once()
        self.assertEqual(self.q.head()[1],'B');self.assertEqual(self.q.status()['acked'],1)

class NtripTest(unittest.TestCase):
    def test_fragmented_header_preserves_initial_rtcm(self):
        sock=Mock();sock.recv.side_effect=[b'HTTP/1.0 2',b'00 OK\r\nContent-Type: gnss/data\r\n\r\n\xd3abc']
        self.assertEqual(read_header(sock),b'\xd3abc')
    def test_icy_and_rejection(self):
        sock=Mock();sock.recv.return_value=b'ICY 200 OK\r\n\xd3abc';self.assertEqual(read_header(sock),b'\xd3abc')
        sock.recv.return_value=b'HTTP/1.0 401 Unauthorized\r\n\r\n'
        with self.assertRaises(ConnectionError):read_header(sock)

if __name__=='__main__':unittest.main()
