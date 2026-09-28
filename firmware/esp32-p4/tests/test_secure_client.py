"""Verify certificate pinning fails before requests and pairing binds the TLS peer."""
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from gateway_client import Gateway, auth_headers, image_signature


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.key=self.folder/'test.key'
        self.key.write_text('01'*32);self.key.chmod(0o600)
        self.pin=self.folder/'pin.json';self.mac='02:00:00:00:00:01'

    def gateway(self):
        with patch('gateway_client.socket.getaddrinfo',return_value=[(2,1,6,'',('192.0.2.2',443))]):
            return Gateway('192.0.2.2',self.pin,self.key,self.mac)

    def test_pin_rejects_before_request(self):
        self.pin.write_text(json.dumps({'schema':1,'ethernet_mac':self.mac,'certificate_sha256':'0'*64}))
        client=self.gateway();connection=MagicMock();connection.sock.getpeercert.return_value=b'untrusted certificate'
        with patch('gateway_client.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaisesRegex(ValueError,'certificate differs'):client.raw('/api/status')
        connection.request.assert_not_called();connection.close.assert_called_once()

    def test_pair_rejects_proof_for_other_certificate(self):
        client=self.gateway();nonce='02'*32;real='a'*64;attacker='b'*64
        context=f'IQDATA-TLS-PAIR-V1\n{nonce}\n{self.mac}\n{real}\n'
        reply={'ethernet_mac':self.mac,'certificate_sha256':real,'proof':hmac.new(bytes.fromhex('01'*32),context.encode(),hashlib.sha256).hexdigest()}
        with patch('gateway_client.secrets.token_hex',return_value=nonce),patch.object(client,'raw',return_value=(200,json.dumps(reply).encode(),attacker)):
            with self.assertRaisesRegex(ValueError,'could not prove'):client.pair()
        self.assertFalse(self.pin.exists())

    def test_pair_saves_only_authenticated_identity(self):
        client=self.gateway();nonce='02'*32;fingerprint='a'*64
        context=f'IQDATA-TLS-PAIR-V1\n{nonce}\n{self.mac}\n{fingerprint}\n'
        reply={'ethernet_mac':self.mac,'certificate_sha256':fingerprint,'proof':hmac.new(bytes.fromhex('01'*32),context.encode(),hashlib.sha256).hexdigest()}
        with patch('gateway_client.secrets.token_hex',return_value=nonce),patch.object(client,'raw',return_value=(200,json.dumps(reply).encode(),fingerprint)):
            self.assertEqual(client.pair()['certificate_sha256'],fingerprint)
        self.assertEqual(self.pin.stat().st_mode&0o777,0o600)
        self.assertNotIn('01'*32,self.pin.read_text())

    def test_manifest_cannot_change_target_or_bytes(self):
        image=self.folder/'image.uf2';image.write_bytes(b'test payload')
        manifest={'schema':1,'target':'pico2','bytes':12,'sha256':hashlib.sha256(b'test payload').hexdigest(),'signature':'01'*70}
        Path(str(image)+'.sig.json').write_text(json.dumps(manifest))
        self.assertEqual(image_signature(image,'pico2')[0],b'test payload')
        with self.assertRaises(ValueError):image_signature(image,'esp32p4')
        image.write_bytes(b'evil payload')
        with self.assertRaises(ValueError):image_signature(image,'pico2')

    def test_request_context_binds_path_nonce_and_body(self):
        args=(b'k'*32,'/api/config','a'*32,b'{}')
        original=auth_headers(*args)['X-IQ-Auth']
        for index,value in ((1,'/api/firmware'),(2,'b'*32),(3,b'{ }')):
            changed=list(args);changed[index]=value
            self.assertNotEqual(original,auth_headers(*changed)['X-IQ-Auth'])


if __name__=='__main__':unittest.main()
