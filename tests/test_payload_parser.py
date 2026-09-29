import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.payload_parser import parse_notification


class ParseNotificationTests(unittest.TestCase):
    def test_parse_json_lead_id(self):
        body = b'{"lead_id": "00Q5f000001AbCdEAK"}'
        result = parse_notification(body, "application/json")
        self.assertEqual(result.lead_ids, ["00Q5f000001AbCdEAK"])
        self.assertFalse(result.is_outbound_message)

    def test_parse_json_nested_record_id(self):
        body = b'{"data": {"recordId": "00Q5f000001AbCdEAK", "other": "x"}}'
        result = parse_notification(body, "application/json")
        self.assertEqual(result.lead_ids, ["00Q5f000001AbCdEAK"])

    def test_parse_json_ignores_non_lead_ids(self):
        body = b'{"account_id": "0015f00000AbCdEAK"}'
        result = parse_notification(body, "application/json")
        self.assertEqual(result.lead_ids, [])

    def test_parse_outbound_message_xml(self):
        body = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            b"<soapenv:Body>"
            b'<notifications xmlns="http://soap.sforce.com/2005/09/outbound">'
            b"<OrganizationId>00D5f000000AbCd</OrganizationId>"
            b"<Notification>"
            b"<Id>04l5f000000AbCd</Id>"
            b'<sObject xsi:type="sf:Lead" xmlns:sf="urn:sobject.enterprise.soap.sforce.com">'
            b"<sf:Id>00Q5f000001AbCdEAK</sf:Id>"
            b"</sObject>"
            b"</Notification>"
            b"</notifications>"
            b"</soapenv:Body>"
            b"</soapenv:Envelope>"
        )
        result = parse_notification(body, "text/xml")
        self.assertTrue(result.is_outbound_message)
        self.assertIn("00Q5f000001AbCdEAK", result.lead_ids)


if __name__ == "__main__":
    unittest.main()
