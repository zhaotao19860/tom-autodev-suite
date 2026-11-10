"""cmdb_query.py 单元测试（mock urllib，无网络）。

运行：在 skill 目录下 python3 -m unittest tests/test_cmdb_query.py
"""
import base64
import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "cmdb_query.py")
_spec = importlib.util.spec_from_file_location("cmdb_query", SCRIPT)
cq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cq)

TEST_SECRET = "s3cr3t-TEST-value-xyz"
TEST_ID = "12345"
ENV_TEXT = (
    "BSP_USER_ID=%s\nBSP_USER_SECRET=%s\nBSP_JWT_ISSUER=test-iss\nBSP_JWT_EXPIRE=600\n"
    "BSP_JWT_HEADER=Authorization\nBSP_ENDPOINT=http://cmdb.example.test/\n" % (TEST_ID, TEST_SECRET)
)


def b64d(seg):
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


class FakeResp:
    def __init__(self, obj):
        self._raw = json.dumps(obj).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = os.path.join(self.tmp.name, "bsp.env")
        with open(self.env_file, "w") as fh:
            fh.write(ENV_TEXT)
        os.chmod(self.env_file, 0o600)
        self.env_patch = mock.patch.dict(os.environ, {"TOM_DIAGNOSE_BSP_ENV": self.env_file})
        self.env_patch.start()
        cq._SENSITIVE.clear()
        self.requests = []

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def run_main(self, argv, responder=None):
        """responder(req) -> 返回 dict（作为 JSON 响应）或抛异常。"""
        def fake_urlopen(req, timeout=None):
            self.requests.append(req)
            result = responder(req) if responder else {"code": "", "data": {"list": []}}
            if isinstance(result, BaseException):
                raise result
            return FakeResp(result)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cq.urllib.request, "urlopen", side_effect=fake_urlopen), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cq.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def query(req):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))


class TestJWT(Base):
    def test_token_fields_and_signature(self):
        creds = cq.load_credentials()
        token = cq.make_jwt(creds, now=1700000000)
        h, p, s = token.split(".")
        self.assertNotIn("=", token)
        self.assertEqual(json.loads(b64d(h)), {"alg": "HS256", "typ": "JWT"})
        self.assertEqual(b64d(h), b'{"alg":"HS256","typ":"JWT"}')
        payload = json.loads(b64d(p))
        self.assertEqual(payload, {"userId": 12345, "iss": "test-iss", "iat": 1700000000,
                                   "nbf": 1700000000, "exp": 1700000600})
        key = ("%s|%s|2" % (TEST_ID, TEST_SECRET)).encode()
        expect = hmac.new(key, ("%s.%s" % (h, p)).encode(), hashlib.sha256).digest()
        self.assertEqual(b64d(s), expect)

    def test_header_sent_without_bearer(self):
        code, _, _ = self.run_main(["nodes", "c1"])
        self.assertEqual(code, 0)
        auth = self.requests[0].get_header("Authorization")
        self.assertEqual(auth.count("."), 2)
        self.assertFalse(auth.startswith("Bearer"))

    def test_secret_and_token_not_leaked_on_401(self):
        def responder(req):
            tok = req.get_header("Authorization")
            body = io.BytesIO(("unauthorized token=%s secret=%s" % (tok, TEST_SECRET)).encode())
            return urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, body)

        code, out, err = self.run_main(["nodes", "c1"], responder)
        self.assertEqual(code, 3)
        token = self.requests[0].get_header("Authorization")
        self.assertIn("401", err)
        for blob in (out, err):
            self.assertNotIn(TEST_SECRET, blob)
            self.assertNotIn(token, blob)
            self.assertNotIn(token.split(".")[2], blob)


class TestCredentials(Base):
    def test_missing_file(self):
        os.remove(self.env_file)
        code, out, err = self.run_main(["nodes", "c1"])
        self.assertEqual(code, 5)
        self.assertIn("BSP_USER_SECRET", err)
        self.assertIn("600", err)
        self.assertEqual(self.requests, [])

    def test_mode_0644_rejected(self):
        os.chmod(self.env_file, 0o644)
        code, out, err = self.run_main(["nodes", "c1"])
        self.assertEqual(code, 5)
        self.assertNotIn(TEST_SECRET, out + err)
        self.assertEqual(self.requests, [])

    def test_mode_0600_ok(self):
        code, _, _ = self.run_main(["nodes", "c1"])
        self.assertEqual(code, 0)
        self.assertTrue(self.requests[0].full_url.startswith(
            "http://cmdb.example.test/cmdb/open/v1/bgw/node?"))


class TestTime(unittest.TestCase):
    def test_absolute_cst(self):
        self.assertEqual(cq.parse_time("2026-11-09 17:07"), 1794215220)
        self.assertEqual(cq.parse_time("2026-11-09 17:07:30"), 1794215250)

    def test_relative(self):
        self.assertEqual(cq.parse_time("now-2h", now=1794215220), 1794215220 - 7200)
        self.assertEqual(cq.parse_time("now-30m", now=1000000000), 1000000000 - 1800)
        self.assertEqual(cq.parse_time("now-1d", now=1000000000), 1000000000 - 86400)
        self.assertEqual(cq.parse_time("now", now=1000000000), 1000000000)
        self.assertEqual(cq.parse_time("1794215220"), 1794215220)

    def test_bad(self):
        with self.assertRaises(cq.CmdbError) as ctx:
            cq.parse_time("yesterday")
        self.assertEqual(ctx.exception.code, 2)

    def test_format(self):
        self.assertEqual(cq.fmt_time(1794215220), "2026-11-09 17:07:00(+08)")
        self.assertEqual(cq.fmt_time(1794215220000), "2026-11-09 17:07:00(+08)")


class TestVslog(Base):
    START = 1794189600  # 2026-11-09 10:00 +08

    def test_chunking_three_hours(self):
        def responder(req):
            q = self.query(req)
            return {"code": "", "data": {"total": 1, "list": [
                {"startTime": int(q["startTime"]) + 5, "endTime": int(q["startTime"]) + 5,
                 "config": [], "rs": [{"type": 1, "rip": "10.0.0.%d" % (len(self.requests)),
                                       "rport": 80, "weight": 1}]}]}}

        code, out, err = self.run_main(
            ["vslog", "--vip", "10.1.1.1", "--port", "80", "--protocol", "tcp",
             "--from", str(self.START), "--to", str(self.START + 3 * 3600), "--chunk", "3600"],
            responder)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.requests), 3)
        for i, req in enumerate(self.requests):
            q = self.query(req)
            self.assertTrue(req.full_url.split("?")[0].endswith("/nlb/vs/log"))
            self.assertEqual(int(q["startTime"]), self.START + i * 3600)
            self.assertEqual(int(q["endTime"]), self.START + (i + 1) * 3600)
            self.assertEqual(q["vni"], "-1")
            self.assertEqual(q["protocol"], "1")
            self.assertEqual(q["vport"], "80")
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("+10.0.0.1:80(1)", lines[0])
        self.assertIn("+10.0.0.2:80(1)", lines[1])
        self.assertIn("+10.0.0.3:80(1)", lines[2])
        self.assertTrue(lines[0].startswith("2026-11-09 10:00:05(+08)"))
        self.assertEqual(err.count("[vslog]"), 3)

    def test_record_format(self):
        rec = {"startTime": 1794215220,
               "config": [{"type": 1, "name": "a", "value": "1"},
                          {"type": 2, "name": "b", "value": "2", "originValue": "1"},
                          {"type": 3, "name": "c"}],
               "rs": [{"type": 1, "rip": "1.1.1.1", "rport": 80, "weight": 5},
                      {"type": 2, "rip": "1.1.1.2", "rport": 80, "weight": 0},
                      {"type": 3, "rip": "1.1.1.3", "rport": 80}]}
        self.assertEqual(cq.format_vslog_record(rec),
                         "2026-11-09 17:07:00(+08) cfg[+a=1 / ~b: 1->2 / -c] "
                         "rs[+1.1.1.1:80(5) ~1.1.1.2:80(0) -1.1.1.3:80]")

    def test_summary(self):
        def responder(req):
            q = self.query(req)
            s = int(q["startTime"])
            return {"code": "", "data": {"list": [
                {"startTime": s + 1, "config": [{"type": 2, "name": "x"}],
                 "rs": [{"type": 1}, {"type": 3}, {"type": 3}]}]}}

        code, out, err = self.run_main(
            ["vslog", "--vs-id", "77", "--from", str(self.START), "--to", str(self.START + 7200),
             "--summary"], responder)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.query(self.requests[0])["vsId"], "77")
        self.assertNotIn("vni", self.query(self.requests[0]))
        self.assertIn("2026-11-09 10:00", out)
        self.assertIn("2026-11-09 11:00", out)
        total = [l for l in out.splitlines() if l.startswith("TOTAL")][0].split()
        self.assertEqual(total, ["TOTAL", "2", "2", "4", "0", "2"])

    def test_validate_error(self):
        code, out, err = self.run_main(
            ["vslog", "--vs-id", "1", "--from", str(self.START), "--to", str(self.START + 60)],
            lambda req: {"code": "ValidateError", "message": "vni is required", "data": None})
        self.assertEqual(code, 3)
        self.assertIn("ValidateError: vni is required", err)

    def test_range_cap(self):
        args = ["vslog", "--vs-id", "1", "--from", str(self.START),
                "--to", str(self.START + 8 * 86400)]
        code, out, err = self.run_main(args)
        self.assertEqual(code, 2)
        self.assertIn("--force-range", err)
        self.assertEqual(self.requests, [])
        code, _, err = self.run_main(args + ["--force-range", "--chunk", "86400"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.requests), 8)

    def test_timeout_is_network_error(self):
        code, out, err = self.run_main(
            ["vslog", "--vs-id", "1", "--from", str(self.START), "--to", str(self.START + 60)],
            lambda req: urllib.error.URLError("timed out"))
        self.assertEqual(code, 4)


class TestVs(Base):
    VS_LIST = [
        {"vsId": 1, "vip": "10.1.1.1", "vport": 80, "protocol": 1, "cluster": "c1",
         "config": {"b": 2, "a": 1}, "rsList": [{"rip": "2.2.2.2", "rport": 8080, "weight": 1}]},
        {"vsId": 2, "vip": "10.1.1.1", "vport": 80, "protocol": 2, "cluster": "c1", "rsList": []},
        {"vsId": 3, "vip": "10.1.1.1", "vport": 443, "protocol": 1, "cluster": "c1", "rsList": []},
    ]

    def responder(self, req):
        return {"code": "", "data": {"total": 3, "list": self.VS_LIST}}

    def ids(self, out):
        return [l.split()[0] for l in out.strip().splitlines()[1:] if l and l[0].isdigit()]

    def test_filter_port_protocol(self):
        code, out, err = self.run_main(["vs", "--vip", "10.1.1.1", "--port", "80",
                                        "--protocol", "tcp"], self.responder)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids(out), ["1"])
        self.assertIn("/nlb/vs-via-vip", self.requests[0].full_url)
        self.assertEqual(self.query(self.requests[0])["vip"], "10.1.1.1")

    def test_filter_port_only(self):
        code, out, _ = self.run_main(["vs", "--vip", "10.1.1.1", "--port", "80"], self.responder)
        self.assertEqual(self.ids(out), ["1", "2"])

    def test_filter_protocol_udp(self):
        code, out, _ = self.run_main(["vs", "--vip", "10.1.1.1", "--protocol", "udp"], self.responder)
        self.assertEqual(self.ids(out), ["2"])

    def test_rs_and_config(self):
        code, out, _ = self.run_main(["vs", "--vip", "10.1.1.1", "--port", "80", "--protocol",
                                      "tcp", "--rs", "--config"], self.responder)
        self.assertIn("rs 2.2.2.2:8080 weight=1", out)
        self.assertLess(out.index("cfg a=1"), out.index("cfg b=2"))
        self.assertEqual(self.query(self.requests[0])["isReturnConfig"], "1")

    def test_via_rip(self):
        code, out, _ = self.run_main(["vs", "--rip", "2.2.2.2", "--rport", "8080"], self.responder)
        self.assertEqual(code, 0)
        self.assertIn("/nlb/vs-via-rip", self.requests[0].full_url)
        self.assertEqual(self.query(self.requests[0]), {"rip": "2.2.2.2", "rport": "8080"})


class TestHostAndArgs(Base):
    def test_host_post_body(self):
        def responder(req):
            return {"code": "", "data": {"list": [
                {"hostname": "h1.yq01", "ip": "1.2.3.4", "cluster": "c1",
                 "service": {"status": "online", "subVersion": "s", "timeVersion": "t",
                             "capacity": 100, "config": "CONF-TEXT", "routeConfig": "ROUTE-TEXT"}}]}}

        code, out, err = self.run_main(["host", "h1.yq01", "h2.yq01"], responder)
        self.assertEqual(code, 0, err)
        req = self.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/cmdb/open/v1/host/service"))
        self.assertEqual(json.loads(req.data), {"project": 1, "hostnameList": ["h1.yq01", "h2.yq01"]})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertIn("1.2.3.4", out)
        self.assertNotIn("CONF-TEXT", out)
        self.assertIn("h2.yq01", err)
        code, out, _ = self.run_main(["host", "h1.yq01", "--show-config", "--show-route"], responder)
        self.assertIn("CONF-TEXT", out)
        self.assertIn("ROUTE-TEXT", out)

    def test_rejects_quote(self):
        for argv in (["nodes", "a'b"], ["host", "a'b"], ["cluster", "a'b"], ["vip", "a'b"],
                     ["vs", "--vip", "a'b"],
                     ["vslog", "--vip", "a'b", "--port", "1", "--protocol", "tcp",
                      "--from", "now-1h", "--to", "now"]):
            code, out, err = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("非法字符", err)
        self.assertEqual(self.requests, [])

    def test_argparse_error_exit_2(self):
        code, _, _ = self.run_main(["vs", "--vip", "1.1.1.1", "--protocol", "icmp"])
        self.assertEqual(code, 2)

    def test_cluster_list(self):
        code, out, err = self.run_main(["cluster"], lambda req: {"code": "", "data": {"list": [
            {"name": "b"}, {"name": "a"}]}})
        self.assertEqual(out.split(), ["a", "b"])
        self.assertNotIn("cluster=", self.requests[0].full_url)

    def test_json_output(self):
        data = {"total": 1, "list": [{"name": "n1"}]}
        code, out, _ = self.run_main(["nodes", "c1", "--json"], lambda req: {"code": "", "data": data})
        self.assertEqual(json.loads(out), data)


if __name__ == "__main__":
    unittest.main()
