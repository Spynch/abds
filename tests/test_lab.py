"""Validation tests and opt-in integration checks against a freshly loaded lab."""
import json
import os
import unittest
from datetime import datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.db import Database, configure
from app.server import ApiError, day, timestamp, uint


class ValidationTests(unittest.TestCase):
    """Check data types, calendar boundaries and monotonic row versions."""

    def test_unsigned_integer(self):
        for value in (True, -1, 1.5, '1', None, 2**32):
            with self.subTest(value=value), self.assertRaises(ApiError):
                uint(value, 'day_kw', 32)
        self.assertEqual(uint(0, 'day_kw', 32), 0)

    def test_strict_dates(self):
        self.assertEqual(day('2024-02-29', 'date'), '2024-02-29')
        for value in ('2023-02-29', '2026-2-01', '20261001', '', None):
            with self.subTest(value=value), self.assertRaises(ApiError):
                day(value, 'date')

    def test_monotonic_version(self):
        self.assertGreater(datetime.fromisoformat(timestamp('2090-01-01 00:00:00')),
                           datetime(2090, 1, 1))


@unittest.skipUnless(os.environ.get('INTEGRATION') == '1', 'Set INTEGRATION=1 after setup')
class IntegrationTests(unittest.TestCase):
    """Exercise HTTP and verify persisted SCD behavior with SQL."""

    @classmethod
    def setUpClass(cls):
        configure()
        cls.url = 'http://localhost:' + os.environ['APP_PORT']
        cls.db = Database()

    def request(self, method, path, body=None, expected=200):
        data = None if body is None else json.dumps(body).encode()
        req = Request(self.url + path, data=data, method=method,
                      headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(req, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            self.assertEqual(response.status, expected, raw)
            return json.loads(raw) if raw else None

    def test_dataset(self):
        expected = {'dim_realty': 1000, 'dim_gauges': 1100, 'dim_users': 1000,
                    'dim_tariffs': 2, 'fact_gauge_bindings': 1000,
                    'fact_meter_readings': 1096000, 'fact_invoice': 36000}
        for table, count in expected.items():
            with self.subTest(table=table):
                final = '' if table in ('dim_realty', 'dim_gauges', 'fact_invoice') else ' FINAL'
                self.assertEqual(self.db.rows(f'SELECT count() n FROM {table}{final}')[0]['n'], count)
        self.assertEqual(self.db.rows('SELECT count() n FROM fact_invoice i INNER JOIN '
                                     '(SELECT * FROM dim_tariffs FINAL) t ON i.tariff_id=t.id '
                                     'WHERE i.created_at>=t.valid_from AND i.created_at<t.valid_to '
                                     'AND i.price != i.day_kw*t.price_day_kwph+i.night_kw*t.price_night_kwph')[0]['n'], 0)

    def test_ping_and_errors(self):
        self.assertIsNone(self.request('GET', '/ping'))
        for path in ('/users/999999', '/realty/999999', '/tariffs?date=1900-01-01'):
            self.assertEqual(self.request('GET', path, expected=404), {'message': 'Not found'})
        for path in ('/tariffs', '/tariffs?date=2023-02-29', '/realty/1/invoices?limit=-1',
                     '/realty/1/readings?date_from=2026-10-31&date_to=2026-10-01'):
            self.assertIn('message', self.request('GET', path, expected=400))
        self.request('PATCH', '/users/1', {}, 400)
        self.request('POST', '/realty/1/readings', {'day_kw': True}, 400)
        self.request('POST', '/realty/1/gauges', {'gauge_id': 999999}, 404)

    def test_scd_users(self):
        old = self.request('GET', '/users/950')
        self.request('PATCH', '/users/950', {'fio': 'New name'}, 204)
        for phone in ('+79990000001', '+79990000002'):
            self.request('PATCH', '/users/950', {'phone': phone}, 204)
        user = self.request('GET', '/users/950')
        self.assertEqual(user['fio'], 'New name')
        self.assertEqual(user['phone'], '+79990000002')
        self.assertEqual(user['phone_prev'], '+79990000001')
        self.request('PATCH', '/users/950', {'phone': user['phone']}, 204)
        self.assertEqual(self.request('GET', '/users/950')['phone_prev'], '+79990000001')
        self.assertEqual(self.db.rows('SELECT count() n FROM dim_users WHERE id=950 AND fio={old:String}',
                                      {'old': old['fio']})[0]['n'], 0)

    def test_tariff_boundary_and_invoices(self):
        before = self.request('GET', '/tariffs?date=2025-10-31')
        after = self.request('GET', '/tariffs?date=2025-11-01')
        self.assertEqual(before['valid_to'], after['valid_from'])
        self.assertNotEqual(before['price_day_kwph'], after['price_day_kwph'])
        page = self.request('GET', '/realty/1/invoices?limit=2&offset=1')
        self.assertEqual(page['count'], 36)
        self.assertEqual(len(page['invoices']), 2)
        self.assertGreater(page['invoices'][0]['created_at'], page['invoices'][1]['created_at'])
        self.assertEqual(self.request('GET', '/realty/1/invoices?limit=0')['invoices'], [])

    def test_reading_correction(self):
        body = dict(user_id=900, gauge_id=900, tariff_id=1,
                    reading_date='2026-10-01', day_kw=123, night_kw=12)
        self.request('POST', '/realty/900/readings', body, 201)
        body['day_kw'] = 456
        self.request('POST', '/realty/900/readings', body, 201)
        result = self.request('GET', '/realty/900/readings?date_from=2026-10-01&date_to=2026-10-01')
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['readings'][0]['day_kw'], 456)
        for value in (True, -1, 1.5, 2**32):
            body['day_kw'] = value
            self.request('POST', '/realty/900/readings', body, 400)

    def test_gauge_replacement(self):
        for gauge in (1099, 1100):
            self.request('POST', '/realty/900/gauges', {'gauge_id': gauge}, 201)
        rows = self.db.rows('SELECT * FROM fact_gauge_bindings FINAL WHERE realty_id=900')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['gauge_id'], 1100)
        self.assertEqual(rows[0]['version'], 3)


if __name__ == '__main__':
    unittest.main()
