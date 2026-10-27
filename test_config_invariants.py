"""Only two official transport state fields may differ; policy must not."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from tools.config_invariants import load_config, static_config, static_fingerprint


def fixture():
    return {'platform': [{'type': 'weixin_oc', 'id': 'synthetic-instance', 'enable': True,
                          'weixin_oc_account_id': 'synthetic-account', 'weixin_oc_token': 'synthetic-login',
                          'weixin_oc_base_url': 'https://synthetic.invalid', 'weixin_oc_sync_buf': 'synthetic-before',
                          'weixin_oc_context_tokens': {'synthetic-user': 'synthetic-context-before'}}],
            'platform_settings': {'id_whitelist': ['synthetic-private']},
            'provider_settings': {'enable': True}, 'dashboard': {'password': 'synthetic-placeholder'}}


class ConfigInvariantTests(unittest.TestCase):
    def test_official_transport_cursor_and_reply_context_can_advance(self):
        before = fixture()
        after = copy.deepcopy(before)
        after['platform'][0]['weixin_oc_sync_buf'] = 'synthetic-next'
        after['platform'][0]['weixin_oc_context_tokens']['synthetic-user'] = 'synthetic-next-context'
        after['platform'][0]['weixin_oc_context_tokens']['synthetic-other-user'] = 'synthetic-new-context'
        self.assertEqual(static_fingerprint(before), static_fingerprint(after))

    def test_identity_login_url_and_enable_remain_strict(self):
        before = fixture()
        for key in ('id', 'weixin_oc_account_id', 'weixin_oc_token', 'weixin_oc_base_url', 'enable', 'type'):
            after = copy.deepcopy(before)
            after['platform'][0][key] = False if key == 'enable' else 'synthetic-changed'
            with self.subTest(key=key):
                self.assertNotEqual(static_fingerprint(before), static_fingerprint(after))

    def test_allowlist_password_and_model_policy_remain_strict(self):
        before = fixture()
        for key in ('platform_settings', 'dashboard', 'provider_settings'):
            after = copy.deepcopy(before)
            after[key] = {'synthetic_changed': True}
            self.assertNotEqual(static_fingerprint(before), static_fingerprint(after))

    def test_unknown_future_fields_are_not_discarded(self):
        before = fixture()
        after = copy.deepcopy(before)
        after['platform'][0]['new_sensitive_setting'] = 'synthetic-new'
        self.assertNotEqual(static_fingerprint(before), static_fingerprint(after))

    def test_other_adapters_do_not_get_mutable_field_exemptions(self):
        before = fixture()
        before['platform'][0]['type'] = 'other-synthetic-adapter'
        after = copy.deepcopy(before)
        after['platform'][0]['weixin_oc_sync_buf'] = 'synthetic-changed'
        self.assertNotEqual(static_fingerprint(before), static_fingerprint(after))

    def test_platform_add_remove_or_reorder_is_a_policy_change(self):
        before = fixture()
        after = copy.deepcopy(before)
        after['platform'].append({'type': 'synthetic-other', 'id': 'synthetic-extra'})
        self.assertNotEqual(static_fingerprint(before), static_fingerprint(after))
        other = copy.deepcopy(after)
        other['platform'].reverse()
        self.assertNotEqual(static_fingerprint(after), static_fingerprint(other))

    def test_invalid_cursor_or_reply_context_types_are_refused(self):
        for key, values in [('weixin_oc_sync_buf', [None, 1, {}, []]),
                            ('weixin_oc_context_tokens', [None, [], '', {'synthetic': 1}])]:
            for value in values:
                after = fixture()
                after['platform'][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    static_fingerprint(after)

    def test_malformed_platform_shape_is_refused(self):
        for value in (None, [], {}, {'platform': {}}, {'platform': [None]}):
            with self.assertRaises(ValueError):
                static_fingerprint(value)

    def test_input_is_not_modified(self):
        original = fixture()
        expected = copy.deepcopy(original)
        static_config(original)
        self.assertEqual(original, expected)

    def test_whitespace_and_key_order_do_not_change_static_policy(self):
        before = fixture()
        after = json.loads(json.dumps(before, sort_keys=True, indent=4))
        self.assertEqual(static_fingerprint(before), static_fingerprint(after))

    def test_duplicate_or_nonfinite_json_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='config-policy-test-') as scratch:
            file = Path(scratch) / 'synthetic.json'
            for value in ('{"platform":[],"platform":[]}', '{"platform":[],"value":NaN}'):
                file.write_text(value)
                with self.assertRaises(ValueError):
                    load_config(file)

    def test_private_snapshot_file_loads_without_exporting_values(self):
        with tempfile.TemporaryDirectory(prefix='config-policy-test-') as scratch:
            file = Path(scratch) / 'synthetic.json'
            file.write_text('\ufeff' + json.dumps(fixture()))
            self.assertEqual(static_fingerprint(load_config(file)), static_fingerprint(fixture()))


if __name__ == '__main__':
    unittest.main()
