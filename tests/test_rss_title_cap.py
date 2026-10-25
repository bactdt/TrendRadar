# coding=utf-8
"""
Telegram 中转源 RSS 标题长度限制测试

背景：Telegram 频道消息没有独立标题，中转服务（如 rsssumo.site/telegram/...）
可能把整条消息文本作为 RSS <title>，曾导致 AI 对话/提示词全文泄漏进推送。

覆盖：
1. is_telegram_feed_url 识别 Telegram 中转源
2. RSSFetcher.from_config 解析单源 max_title_length 配置
3. RSSParser 标题截断（实例默认 / parse() 覆盖参数）
"""

import json
import sys
import types
import unittest

# 本测试支持在未安装 feedparser 的环境下运行：
# 桩掉 feedparser 仅用于跳过 import 崩溃，真实验证走 JSON Feed 路径（仅用标准库）。
try:
    import feedparser  # noqa: F401
    HAS_FEEDPARSER = True
except ImportError:
    _stub = types.ModuleType("feedparser")

    def _stub_parse(_content):
        raise RuntimeError("stub feedparser 不用于本测试")

    _stub.parse = _stub_parse
    sys.modules["feedparser"] = _stub
    HAS_FEEDPARSER = False

# trendradar 顶层 __init__ 会拉起 litellm 等运行时依赖，测试环境缺失时同样桩掉
for _optional_runtime in ("litellm", "fastmcp", "websockets"):
    try:
        __import__(_optional_runtime)
    except ImportError:
        _runtime_stub = types.ModuleType(_optional_runtime)
        if _optional_runtime == "litellm":
            def _stub_completion(*_args, **_kwargs):
                raise RuntimeError("stub litellm 不用于本测试")

            _runtime_stub.completion = _stub_completion
        sys.modules[_optional_runtime] = _runtime_stub

from trendradar.crawler.rss.fetcher import (
    TELEGRAM_MAX_TITLE_LENGTH,
    RSSFetcher,
    is_telegram_feed_url,
)
from trendradar.crawler.rss.parser import RSSParser



def _json_feed(*titles):
    data = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "test",
        "items": [
            {"id": f"t{i}", "url": f"https://example.org/{i}", "title": t}
            for i, t in enumerate(titles)
        ],
    }
    return json.dumps(data, ensure_ascii=False)


class TelegramURLTest(unittest.TestCase):
    def test_detect_relay_urls(self):
        self.assertTrue(is_telegram_feed_url("https://rsssumo.site/telegram/channel/zaihuapd"))
        self.assertTrue(is_telegram_feed_url("http://t.me/s/somechannel"))
        self.assertTrue(is_telegram_feed_url("https://example.org/TELEGRAM/feed.xml"))

    def test_non_telegram_urls(self):
        self.assertFalse(is_telegram_feed_url("https://hnrss.org/frontpage"))
        self.assertFalse(is_telegram_feed_url("http://www.ruanyifeng.com/blog/atom.xml"))
        self.assertFalse(is_telegram_feed_url(""))
        self.assertFalse(is_telegram_feed_url(None))

    def test_default_telegram_cap_constant(self):
        # 正常 TG 频道标题实测最长 ~83 字，泄漏文本 >1000 字，默认值取两者之间
        self.assertEqual(TELEGRAM_MAX_TITLE_LENGTH, 128)


class FetcherConfigTest(unittest.TestCase):
    @staticmethod
    def _feeds_from(config):
        return list(RSSFetcher.from_config(config).feeds)

    def test_from_config_parses_max_title_length(self):
        feeds = self._feeds_from({
            "feeds": [
                {"id": "a", "name": "A", "url": "https://example.org/a", "max_title_length": 64},
            ]
        })
        self.assertEqual(feeds[0].max_title_length, 64)

    def test_from_config_invalid_max_title_length_falls_back_to_none(self):
        for raw in (0, -5, "oops"):
            feeds = self._feeds_from({
                "feeds": [
                    {"id": "a", "name": "A", "url": "https://example.org/a", "max_title_length": raw},
                ]
            })
            self.assertIsNone(feeds[0].max_title_length)

    def test_from_config_defaults_to_none(self):
        feeds = self._feeds_from({
            "feeds": [
                {"id": "a", "name": "A", "url": "https://example.org/a"},
            ]
        })
        self.assertIsNone(feeds[0].max_title_length)


class ParserTitleCapTest(unittest.TestCase):
    def setUp(self):
        self.parser = RSSParser()

    def test_apply_title_cap(self):
        cap = 128
        long_text = "任务" * 100  # 200 字，模拟整条 TG 消息文本被当作标题
        capped = self.parser._apply_title_cap(long_text, cap)
        self.assertEqual(len(capped), cap + 3)
        self.assertTrue(capped.endswith("..."))
        self.assertEqual(capped[:cap], long_text[:cap])

    def test_apply_title_cap_noop(self):
        title = "正常长度的新闻标题"
        self.assertEqual(self.parser._apply_title_cap(title, 128), title)
        self.assertEqual(self.parser._apply_title_cap("", 128), "")

    def test_json_feed_instance_cap_applies(self):
        self.parser.max_title_length = 64
        long_title = "字" * 100
        items = self.parser.parse(_json_feed(long_title, "short"), "some-feed")
        self.assertEqual(items[0].title, "字" * 64 + "...")
        self.assertEqual(items[1].title, "short")

    def test_json_feed_override_param_wins(self):
        # 实例默认 64，本次解析覆盖为 32
        self.parser.max_title_length = 64
        long_title = "字" * 100
        items = self.parser.parse(_json_feed(long_title), "some-feed", max_title_length=32)
        self.assertEqual(items[0].title, "字" * 32 + "...")

    def test_json_feed_no_cap_by_default(self):
        long_title = "字" * 500
        items = self.parser.parse(_json_feed(long_title), "some-feed")
        self.assertEqual(items[0].title, long_title)

    @unittest.skipUnless(HAS_FEEDPARSER, "需要安装 feedparser 才能验证 XML Feed 路径")
    def test_xml_feed_cap_applies(self):
        long_title = "D" * 300
        rss_xml = f"""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>t</title>
        <item><title>{long_title}</title><link>https://example.org/1</link></item>
        </channel></rss>"""
        items = self.parser.parse(rss_xml, "some-feed", max_title_length=50)
        self.assertEqual(items[0].title, "D" * 50 + "...")


if __name__ == "__main__":
    unittest.main()
