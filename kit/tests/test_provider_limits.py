"""Контракт распознавания квоты, который можно запускать отдельно от дашборда."""
import unittest

from kit.provider_limits import quota_error


class QuotaClassification(unittest.TestCase):
    def test_structured_and_public_limit_errors(self):
        for error in ({'status': 429}, {'statusCode': 429}, {'code': 429},
                      {'type': 'provider.quota'}, {'name': 'FreeUsageLimitError'},
                      {'message': 'Rate limit exceeded. Please try again later.'},
                      {'message': 'Daily quota exhausted'}, {'message': 'insufficient_quota'}):
            self.assertTrue(quota_error(error), error)
        for error in (None, '429', {'status': True}, {'status': 401}, {'code': 428},
                      {'message': 'Context token limit exceeded'}, {'message': 'Permission denied'},
                      {'payload': 'private 429'}):
            self.assertFalse(quota_error(error), error)
