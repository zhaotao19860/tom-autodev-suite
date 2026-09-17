import subprocess
import unittest

import ipipe_logs

REAL = """2026-09-09 04:01:45 [INFO] Running on bjkjy-sys-ip-base-sep6.bjkjy.baidu.com(10.130.21.26) with agent(v1.2.6) in workspace /root/workspace/021389e4
2026-09-09 04:01:45 + cd /home/work/yueyufei/x86bgw/
2026-09-09 04:01:45 + script/fetch_cr.sh 122402145 --run
2026-09-09 04:01:45 ==> 拉取 refs/changes/45/122402145/4
2026-09-09 04:01:46   [保留] bgw_auto/case_stun_route_tcp/test_stun_route_NAT44_cc_tcp_001.py (评审删除，本地保留)
2026-09-09 04:01:46   [新增] bgw_auto/case_stun_route_tcp/test_stun_route_NAT44_cc_tcp_017.py
2026-09-09 04:16:17     success_ratio: 0.00%
2026-09-09 04:16:17     失败(17): test_stun_route_NAT44_cc_tcp_001 test_stun_route_NAT44_cc_tcp_017 ...(共 17 个)
"""

GATED = """2026-09-09 03:27:51 ++ cat case_stun_route_udp/res.csv
2026-09-09 03:27:51 + ratio6=96.15%
2026-09-09 03:27:51 + ((  96 < 95  ))
"""


class IpipeLogTests(unittest.TestCase):
    def test_a_zero_ratio_run_is_readable_from_the_log(self):
        parsed = ipipe_logs.parse(REAL)

        self.assertEqual(parsed["agent_host"], "bjkjy-sys-ip-base-sep6.bjkjy.baidu.com")
        self.assertEqual(parsed["agent_ip"], "10.130.21.26")
        self.assertEqual(parsed["workspace"], "/root/workspace/021389e4")
        self.assertIn("script/fetch_cr.sh", parsed["scripts"])
        self.assertEqual(parsed["success_ratios"], [0.0])
        self.assertIn("test_stun_route_NAT44_cc_tcp_017", parsed["failed_cases"])

    def test_the_gate_comparison_the_script_itself_makes_is_captured(self):
        parsed = ipipe_logs.parse(GATED)

        self.assertEqual(parsed["success_ratios"], [])
        self.assertEqual(parsed["gates"], [{"actual": 96, "threshold": 95}])
        self.assertEqual(parsed["case_directories"], ["case_stun_route_udp"])

    def test_the_log_is_fetched_with_curl_and_failures_are_reported_not_raised(self):
        calls = []

        def ok(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=REAL, stderr="")

        def refused(argv):
            return subprocess.CompletedProcess(argv, 6, stdout="", stderr="could not resolve host")

        fetched = ipipe_logs.fetch("https://logonline.example/abc", runner=ok)
        failed = ipipe_logs.fetch("https://logonline.example/abc", runner=refused)
        invalid = ipipe_logs.fetch("not-a-url", runner=ok)

        self.assertTrue(fetched["ok"])
        self.assertIn("curl", calls[0][0])
        self.assertEqual(failed["reason_code"], "LOG_FETCH_FAILED")
        self.assertEqual(invalid["reason_code"], "LOG_URL_INVALID")


if __name__ == "__main__":
    unittest.main()
