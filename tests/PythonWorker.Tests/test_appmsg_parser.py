"""Pure app-message parser tests; every XML shape here is synthetic."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from xml.etree import ElementTree
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "PythonWorker"))

from wechat_memory.appmsg_parser import (  # noqa: E402
    APPMSG_PARSER_VERSION, MAX_FORWARD_ENTRIES, MAX_OUTPUT_BYTES,
    MAX_QUOTE_BYTES, MAX_XML_BYTES, MAX_XML_DEPTH, MAX_XML_NODES,
    AppMessageLimits, parse_appmsg,
)


def field(name: str, text: str) -> str:
    return f"<{name}>{escape(text)}</{name}>"


def app(kind: int | None, extra: str = "", *, title: str = "外层标题") -> str:
    type_xml = field("type", str(kind)) if kind is not None else ""
    return f"<msg><appmsg>{type_xml}{field('title', title)}{extra}</appmsg></msg>"


def reference(content: str, *, kind: int = 1, server_id: str = "18446744073709551615",
              sender: str = "被引发送者") -> str:
    return ("<refermsg>" + field("type", str(kind)) + field("svrid", server_id)
            + field("displayname", sender) + field("content", content) + "</refermsg>")


def entry(text: str, *, kind: int = 1, sender: str = "内部发送者",
          timestamp: str = "2026-11-28 09:01:02", extra: str = "") -> str:
    return (f'<dataitem datatype="{kind}"><dataitemsource>'
            + field("sourcename", sender) + field("sourcetime", timestamp)
            + "</dataitemsource>" + field("datadesc", text) + extra + "</dataitem>")


def record(entries: list[str], *, count: str | None = None) -> str:
    count = str(len(entries)) if count is None else count
    return (f'<recordinfo><title>内层标题</title><desc>记录来源</desc><datalist count="{count}">'
            + "".join(entries) + "</datalist></recordinfo>")


def record_field(xml: str, mode: str = "escaped") -> str:
    if mode == "inline":
        return "<recorditem>" + xml + "</recorditem>"
    if mode == "cdata":
        return "<recorditem><![CDATA[" + xml + "]]></recorditem>"
    return field("recorditem", xml)


def forward(entries: list[str], *, mode: str = "escaped", count: str | None = None) -> str:
    return app(19, record_field(record(entries, count=count), mode))


def display_bytes(result) -> int:
    data = asdict(result)
    data.pop("canonical_text")
    data.pop("issues")
    data.pop("parse_status")

    def total(value) -> int:
        if isinstance(value, str):
            return len(value.encode("utf-8"))
        if isinstance(value, dict):
            return sum(total(val) for key, val in value.items() if key != "display_kind")
        if isinstance(value, (tuple, list)):
            return sum(total(val) for val in value)
        return 0
    return total(data)


class ProjectionTests(unittest.TestCase):
    def test_link_canonical_and_local_display_use_outer_fields_only(self):
        xml = app(5, '<refermsg><title>陷阱标题</title><url>inner-url</url></refermsg>'
                  + field("des", "中文 description\n第二行 ✅")
                  + field("url", "https://example.invalid/x?a=1&b=2"))
        result = parse_appmsg(xml)
        self.assertEqual(result.parse_status, "ok")
        self.assertEqual(result.display_kind, "link")
        self.assertEqual(result.title, "外层标题")
        self.assertEqual(result.description, "中文 description\n第二行 ✅")
        self.assertEqual(result.url, "https://example.invalid/x?a=1&b=2")
        self.assertIn(result.url, result.canonical_text)
        self.assertNotIn("陷阱标题", result.canonical_text)
        self.assertNotIn("inner-url", result.canonical_text)
        self.assertEqual(result.omitted_count, 0)
        self.assertFalse(result.truncated)

    def test_appmsg_root_and_namespaced_direct_fields(self):
        for xml in ('<appmsg><type>5</type><title>root</title></appmsg>',
                    '<w:msg xmlns:w="urn:synthetic"><w:appmsg><w:type>5</w:type>'
                    '<w:title>root</w:title></w:appmsg></w:msg>'):
            with self.subTest(xml=xml):
                self.assertEqual(parse_appmsg(xml).title, "root")
                self.assertEqual(parse_appmsg(xml).parse_status, "ok")

    def test_file_retains_filename_zero_or_large_size_without_opening(self):
        for size in (0, 42, 2**64 - 1):
            with self.subTest(size=size):
                xml = app(6, '<appattach><filename>C:\\synthetic\\附件.pdf</filename>'
                          + field("totallen", str(size)) + '</appattach>')
                result = parse_appmsg(xml)
                self.assertEqual(result.display_kind, "file")
                self.assertEqual(result.file_name, "C:\\synthetic\\附件.pdf")
                self.assertEqual(result.file_size, size)
                self.assertIn(f"大小: {size} 字节", result.canonical_text)
                self.assertEqual(result.parse_status, "ok")

    def test_file_title_fallback_and_invalid_sizes(self):
        self.assertEqual(parse_appmsg(app(6)).file_name, "外层标题")
        for value in ("-1", "no", "1.5", "9" * 100, str(2**64), "１２"):
            with self.subTest(size=value):
                result = parse_appmsg(app(6, '<appattach>' + field('totallen', value) + '</appattach>'))
                self.assertIsNone(result.file_size)
                self.assertIn("invalid_file_size", result.issues)
                self.assertEqual(result.parse_status, "partial")

    def test_quote_preserves_long_body_sender_and_numeric_reference(self):
        original = "前" * 90 + "搜索命中关键词 & <literal>"
        result = parse_appmsg(app(57, reference(original), title="当前回复正文"))
        self.assertEqual(result.display_kind, "quote")
        self.assertEqual(result.parse_status, "ok")
        self.assertEqual(result.reply_to, "18446744073709551615")
        self.assertEqual(result.quote_sender, "被引发送者")
        self.assertEqual(result.quote_summary, original)
        self.assertIn("当前回复正文", result.canonical_text)
        self.assertIn("搜索命中关键词", result.canonical_text)

    def test_quote_missing_or_untrusted_id_never_fabricates_relationship(self):
        missing = parse_appmsg(app(57, title="保留回复"))
        self.assertIn("保留回复", missing.canonical_text)
        self.assertIsNone(missing.reply_to)
        self.assertEqual(missing.parse_status, "partial")
        for value in ("", "0", "-2", "123\n456", "path/to/id", "１２", str(2**64)):
            with self.subTest(reference=value):
                result = parse_appmsg(app(57, reference("仍保留引文", server_id=value)))
                self.assertIsNone(result.reply_to)
                self.assertIn("仍保留引文", result.canonical_text)

    def test_quote_media_and_unknown_types_have_visible_placeholders(self):
        image = parse_appmsg(app(57, reference("not-a-file-to-read", kind=3)))
        self.assertEqual(image.quote_summary, "[图片]")
        unknown = parse_appmsg(app(57, reference("SECRET_XML", kind=999)))
        self.assertEqual(unknown.parse_status, "partial")
        self.assertNotIn("SECRET_XML", unknown.canonical_text)
        self.assertIn("不支持", unknown.canonical_text)

    def test_quote_embedded_appmsg_is_parsed_only_at_designated_site(self):
        nested = app(5, field("url", "https://example.invalid"), title="被引卡片")
        result = parse_appmsg(app(57, reference(nested, kind=49)))
        self.assertEqual(result.parse_status, "ok")
        self.assertIn("被引卡片", result.quote_summary)
        self.assertIn("https://example.invalid", result.canonical_text)
        plain = parse_appmsg(app(57, reference(nested, kind=1)))
        self.assertEqual(plain.quote_summary, nested)

    def test_cdata_entities_and_one_extra_embedded_decode_are_bounded(self):
        outer = '<appmsg><type>5</type><title><![CDATA[A &amp; B]]></title></appmsg>'
        self.assertEqual(parse_appmsg(outer).title, "A &amp; B")
        for mode in ("inline", "escaped", "cdata"):
            with self.subTest(mode=mode):
                result = parse_appmsg(forward([entry("A & B <literal>")], mode=mode))
                self.assertEqual(result.parse_status, "ok")
                self.assertEqual(result.forward_items[0].content, "A & B <literal>")
        embedded = escape(record([entry("内容")]))
        once = parse_appmsg(app(19, record_field(embedded, "cdata")))
        self.assertEqual(once.parse_status, "ok")
        twice = parse_appmsg(app(19, record_field(escape(embedded), "cdata")))
        self.assertEqual(twice.parse_status, "partial")
        self.assertEqual(twice.forward_items, ())

    def test_unknown_conflicting_and_missing_types_do_not_guess_mapping(self):
        result = parse_appmsg(app(999, '<secret><title>INNER_SECRET</title></secret>'))
        self.assertEqual(result.display_kind, "unknown")
        self.assertEqual(result.parse_status, "unsupported")
        self.assertIn("外层标题", result.canonical_text)
        self.assertNotIn("INNER_SECRET", result.canonical_text)
        self.assertIsNone(result.omitted_count)
        conflict = parse_appmsg(app(5, reference("must not be interpreted")), (57 << 32) | 49)
        self.assertEqual(conflict.display_kind, "unknown")
        self.assertEqual(conflict.parse_status, "partial")
        self.assertIsNone(conflict.reply_to)
        self.assertIn("type_conflict", conflict.issues)
        self.assertEqual(parse_appmsg(app(5), (5 << 32) | 49).display_kind, "link")
        self.assertEqual(parse_appmsg(app(5), 49).parse_status, "ok")
        self.assertEqual(parse_appmsg(app(None), (5 << 32) | 49).display_kind, "unknown")

    def test_ambiguous_or_nested_fields_cannot_supply_outer_title_or_type(self):
        for fields in ('<title>A</title><title>B</title>',
                       '<title><title>INNER_SECRET</title></title>'):
            result = parse_appmsg('<appmsg><type>5</type>' + fields + '</appmsg>')
            self.assertEqual(result.title, "")
            self.assertEqual(result.parse_status, "partial")
            self.assertNotIn("INNER_SECRET", result.canonical_text)
        result = parse_appmsg('<appmsg><nested><type>5</type><title>INNER_SECRET</title></nested></appmsg>')
        self.assertEqual(result.display_kind, "unknown")
        self.assertNotIn("INNER_SECRET", result.canonical_text)

    def test_unsupported_root_does_not_search_arbitrary_descendants(self):
        result = parse_appmsg('<unrelated>' + app(5) + '</unrelated>')
        self.assertEqual(result.parse_status, "unsupported")
        self.assertNotIn("外层标题", result.canonical_text)

    def test_utf8_bytes_equal_unicode_and_result_is_immutable_json_safe(self):
        xml = app(57, reference("中文 ✅"))
        result = parse_appmsg(xml)
        self.assertEqual(parse_appmsg(xml.encode()), result)
        self.assertEqual(parse_appmsg(b'\xef\xbb\xbf' + xml.encode()), result)
        json.dumps(asdict(result), ensure_ascii=False).encode("utf-8", errors="strict")
        with self.assertRaises(FrozenInstanceError):
            result.title = "changed"
        self.assertTrue(APPMSG_PARSER_VERSION)


class ForwardTests(unittest.TestCase):
    def test_preserves_duplicate_entries_order_source_times_and_outer_boundary(self):
        duplicate = entry("重复内容")
        result = parse_appmsg(forward([duplicate, duplicate, entry("第三条关键词", sender="另一发送者")]))
        self.assertEqual(result.display_kind, "forward")
        self.assertEqual(result.parse_status, "ok")
        self.assertEqual(len(result.forward_items), 3)
        self.assertEqual(result.forward_items[0], result.forward_items[1])
        self.assertEqual(result.forward_items[2].sender, "另一发送者")
        self.assertEqual(result.forward_items[0].timestamp, "2026-11-28 09:01:02")
        self.assertIn("内层标题", result.canonical_text)
        self.assertIn("记录来源", result.canonical_text)
        self.assertIn("第三条关键词", result.canonical_text)
        self.assertEqual(result.canonical_text.count("重复内容"), 2)
        self.assertIsNone(result.reply_to)
        self.assertEqual(result.omitted_count, 0)

    def test_record_media_link_file_and_unknown_entry(self):
        entries = [entry("", kind=kind) for kind in (2, 3, 4, 6, 7)]
        entries += [entry("描述", kind=5, extra=field("datatitle", "链接标题") + field("dataurl", "https://example.invalid")),
                    entry("", kind=8, extra=field("datatitle", "附件.txt") + field("datasize", "123")),
                    entry("DO_NOT_ECHO_UNKNOWN_XML", kind=999, extra=field("datatitle", "未知条目标题"))]
        result = parse_appmsg(forward(entries))
        for label in ("图片", "语音", "视频", "位置", "表情", "链接标题", "附件.txt", "123 字节", "未知条目标题"):
            self.assertIn(label, result.canonical_text)
        self.assertEqual(len(result.forward_items), len(entries))
        self.assertEqual(result.parse_status, "partial")
        self.assertNotIn("DO_NOT_ECHO_UNKNOWN_XML", result.canonical_text)
        self.assertIsNone(result.omitted_count)

    def test_entry_budget_exact_boundary_omitted_count_and_visible_notice(self):
        for count in (MAX_FORWARD_ENTRIES, MAX_FORWARD_ENTRIES + 1):
            with self.subTest(count=count):
                result = parse_appmsg(forward([entry(str(i), sender="", timestamp="") for i in range(count)]))
                self.assertEqual(len(result.forward_items), MAX_FORWARD_ENTRIES)
                self.assertEqual(result.omitted_count, count - MAX_FORWARD_ENTRIES)
                self.assertEqual(result.truncated, count > MAX_FORWARD_ENTRIES)
                self.assertEqual(result.parse_status, "ok" if count == MAX_FORWARD_ENTRIES else "partial")
                if result.truncated:
                    self.assertIn("仅展开前200条", result.canonical_text)
                    self.assertIn("内容已截断", result.canonical_text)

    def test_declared_count_never_invents_an_omitted_total(self):
        limits = AppMessageLimits(max_entries=1)
        result = parse_appmsg(forward([entry("first"), entry("second")], count="999999999"), limits=limits)
        self.assertEqual(result.omitted_count, 1)
        self.assertIn("record_count_mismatch", result.issues)
        self.assertNotIn("999999999", result.canonical_text)

    def test_nested_records_obey_embedded_depth_and_shared_entry_allowance(self):
        nested = entry("", kind=17, extra=record_field(record([entry("nested needle")])) )
        complete = parse_appmsg(forward([nested]))
        self.assertEqual(complete.parse_status, "ok")
        self.assertEqual(complete.forward_items[0].forward_items[0].content, "nested needle")
        self.assertIn("nested needle", complete.canonical_text)
        limited = parse_appmsg(forward([nested]), limits=AppMessageLimits(max_entries=1))
        self.assertEqual(len(limited.forward_items), 1)
        self.assertEqual(limited.forward_items[0].forward_items, ())
        self.assertEqual(limited.omitted_count, 1)
        deeper = entry("", kind=17, extra=record_field(record([nested])))
        limited = parse_appmsg(forward([deeper]))
        self.assertIn("nesting_limit", limited.issues)
        self.assertIsNone(limited.omitted_count)
        self.assertNotIn("nested needle", limited.canonical_text)
        self.assertTrue(limited.truncated)

    def test_skipped_opaque_nested_entry_makes_omitted_count_unknown(self):
        nested = entry("", kind=17, extra=record_field(record([entry("hidden")])) )
        result = parse_appmsg(forward([nested]), limits=AppMessageLimits(max_entries=0))
        self.assertEqual(result.forward_items, ())
        self.assertIsNone(result.omitted_count)
        self.assertIn("entry_limit", result.issues)

    def test_missing_malformed_ambiguous_and_unsupported_records_preserve_outer_title(self):
        extras = ("", record_field("<recordinfo>bad"), record_field("<unknown/>"),
                  record_field("<recordinfo/>") + record_field("<recordinfo/>"),
                  '<recorditem><recordinfo/><recordinfo/></recorditem>')
        for extra in extras:
            with self.subTest(extra=extra):
                result = parse_appmsg(app(19, extra))
                self.assertEqual(result.display_kind, "forward")
                self.assertIn("外层标题", result.canonical_text)
                self.assertEqual(result.parse_status, "partial")
                self.assertIsNone(result.omitted_count)

    def test_recordxml_and_inline_nested_shapes_and_missing_metadata(self):
        nested = '<dataitem datatype="17"><recordxml>' + record([entry("child")]) + '</recordxml></dataitem>'
        result = parse_appmsg(forward([nested, '<dataitem datatype="1"><datadesc>plain</datadesc></dataitem>']))
        self.assertEqual(result.parse_status, "ok")
        self.assertEqual(result.forward_items[1].sender, None)
        self.assertEqual(result.forward_items[1].timestamp, None)
        self.assertIn("child", result.canonical_text)


class SafetyBudgetTests(unittest.TestCase):
    def test_rejects_dtd_entities_and_external_references_without_io(self):
        declarations = (
            '<!DOCTYPE msg [<!ENTITY x "expansion">]>',
            '<!DOCTYPE msg SYSTEM "file:///DO_NOT_OPEN">',
            '<!DOCTYPE msg SYSTEM "https://example.invalid/DO_NOT_FETCH">',
            '<!DOCTYPE msg [<!ENTITY x SYSTEM "file:///DO_NOT_OPEN">]>',
            '<!DOCTYPE msg [<!ENTITY % x SYSTEM "https://example.invalid/x">%x;]>',
            '<!DOCTYPE msg [<!ENTITY a "1234567890"><!ENTITY b "&a;&a;&a;&a;">]>',
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                result = parse_appmsg(declaration + app(5))
                self.assertEqual(result.parse_status, "rejected")
                self.assertIn("forbidden_xml_declaration", result.issues)
                self.assertNotIn("DO_NOT_", result.canonical_text)
                self.assertIsNone(result.omitted_count)
        inner = '<!DOCTYPE recordinfo [<!ENTITY x "expansion">]>' + record([])
        result = parse_appmsg(app(19, record_field(inner)))
        self.assertEqual(result.parse_status, "rejected")
        self.assertIn("外层标题", result.canonical_text)

    def test_declaration_like_literals_and_predefined_entities_are_not_false_positives(self):
        title = '<!DOCTYPE not-a-declaration> & <!ENTITY x "literal">'
        result = parse_appmsg(app(5, title=title))
        self.assertEqual(result.parse_status, "ok")
        self.assertEqual(result.title, title)
        xml = '<appmsg><!-- <!DOCTYPE literal> --><type>5</type><title><![CDATA[<!ENTITY literal>]]></title></appmsg>'
        self.assertEqual(parse_appmsg(xml).parse_status, "ok")

    def test_all_parser_paths_are_file_network_process_and_script_io_free(self):
        payloads = [app(5, field("url", "javascript:alert('never run')")),
                    app(6, '<appattach><filename>file:///DO_NOT_OPEN</filename></appattach>'),
                    app(57, reference("<script>never run</script>")),
                    forward([entry("data", kind=8, extra=field("datatitle", "C:\\DO_NOT_OPEN"))]),
                    '<!DOCTYPE msg SYSTEM "https://example.invalid/NO">' + app(5),
                    app(19, record_field('<!DOCTYPE x SYSTEM "file:///NO"><x/>')),
                    '<?script never-run?>' + app(5)]
        with mock.patch("builtins.open", side_effect=AssertionError("file IO")), \
             mock.patch("os.open", side_effect=AssertionError("file IO")), \
             mock.patch("socket.socket", side_effect=AssertionError("network IO")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("process IO")), \
             mock.patch("os.system", side_effect=AssertionError("script IO")):
            for xml in payloads:
                before = hashlib.sha256(xml.encode()).hexdigest()
                result = parse_appmsg(xml)
                self.assertTrue(result.canonical_text)
                self.assertEqual(hashlib.sha256(xml.encode()).hexdigest(), before)

    def test_byte_budget_at_boundary_and_multibyte_not_character_count(self):
        xml = app(5, title="中" * 100)
        size = len(xml.encode())
        self.assertEqual(parse_appmsg(xml, limits=AppMessageLimits(max_xml_bytes=size)).parse_status, "ok")
        for payload in (xml, xml.encode()):
            result = parse_appmsg(payload, limits=AppMessageLimits(max_xml_bytes=size - 1))
            self.assertEqual(result.parse_status, "limit_exceeded")
            self.assertIn("xml_byte_limit", result.issues)
            self.assertNotIn("中" * 10, result.canonical_text)
        result = parse_appmsg(b"x" * (MAX_XML_BYTES + 1))
        self.assertEqual(result.parse_status, "limit_exceeded")
        self.assertTrue(result.truncated)

    def test_depth_budget_is_enforced_during_tree_construction(self):
        for depth in (MAX_XML_DEPTH, MAX_XML_DEPTH + 1):
            xml = '<x>' * depth + '</x>' * depth
            result = parse_appmsg(xml)
            self.assertEqual(result.parse_status, "unsupported" if depth == MAX_XML_DEPTH else "limit_exceeded")
            if depth > MAX_XML_DEPTH:
                self.assertIn("xml_depth_limit", result.issues)
        result = parse_appmsg('<x>' * 5000 + '</x>' * 5000)
        self.assertEqual(result.parse_status, "limit_exceeded")

    def test_node_budget_boundary_and_aggregate_embedded_xml(self):
        for nodes in (MAX_XML_NODES, MAX_XML_NODES + 1):
            xml = '<x>' + '<v/>' * (nodes - 1) + '</x>'
            result = parse_appmsg(xml)
            self.assertEqual(result.parse_status, "unsupported" if nodes == MAX_XML_NODES else "limit_exceeded")
        xml = forward([entry("nested")])
        outer_nodes = sum(1 for _ in ElementTree.fromstring(xml).iter())
        result = parse_appmsg(xml, limits=AppMessageLimits(max_nodes=outer_nodes))
        self.assertEqual(result.parse_status, "limit_exceeded")
        self.assertIn("xml_node_limit", result.issues)
        self.assertEqual(result.forward_items, ())

    def test_output_cap_includes_labels_and_multibyte_display_fields(self):
        limits = AppMessageLimits(max_output_bytes=256)
        for xml in (app(5, field("des", "中文✅" * 1000), title="标题"),
                    forward([entry("内容✅" * 200) for _ in range(10)])):
            result = parse_appmsg(xml, limits=limits)
            self.assertLessEqual(len(result.canonical_text.encode()), 256)
            self.assertLessEqual(display_bytes(result), 256)
            self.assertTrue(result.truncated)
            self.assertIn("内容已截断", result.canonical_text)
            self.assertEqual(result.parse_status, "partial")
            json.dumps(asdict(result), ensure_ascii=False).encode("utf-8", errors="strict")

    def test_default_output_and_quote_summary_byte_limits(self):
        long = "引文✅" * 1000
        result = parse_appmsg(app(57, reference(long), title="保留当前回复"))
        self.assertLessEqual(len(result.quote_summary.encode()), MAX_QUOTE_BYTES)
        self.assertIn("quote_limit", result.issues)
        self.assertIn("保留当前回复", result.canonical_text)
        self.assertIn("内容已截断", result.canonical_text)
        self.assertTrue(result.truncated)
        result = parse_appmsg(app(5, field("des", "x" * (MAX_OUTPUT_BYTES + 1))))
        self.assertLessEqual(len(result.canonical_text.encode()), MAX_OUTPUT_BYTES)
        self.assertLessEqual(display_bytes(result), MAX_OUTPUT_BYTES)

    def test_zero_nesting_limit_and_nested_quote_share_limit(self):
        result = parse_appmsg(forward([entry("hidden")]), limits=AppMessageLimits(max_nesting=0))
        self.assertEqual(result.forward_items, ())
        self.assertIn("nesting_limit", result.issues)
        inner = app(57, reference(app(57, reference(app(5), kind=49)), kind=49))
        result = parse_appmsg(app(57, reference(inner, kind=49)))
        self.assertIn("nesting_limit", result.issues)
        self.assertTrue(result.truncated)

    def test_invalid_xml_encoding_empty_and_plain_fallback_are_bounded(self):
        values = ("", "<msg>", "<appmsg><title>&missing;</title></appmsg>",
                  b"\xff", "\ud800", '<appmsg>\x00</appmsg>', '<a/><b/>',
                  app(5).encode("utf-16"))
        for value in values:
            with self.subTest(value=repr(value)):
                result = parse_appmsg(value)
                self.assertEqual(result.parse_status, "invalid")
                self.assertTrue(result.canonical_text)
                result.canonical_text.encode("utf-8", errors="strict")
                self.assertLessEqual(len(result.canonical_text.encode()), MAX_OUTPUT_BYTES)
        result = parse_appmsg("plain fallback 中文\x00")
        self.assertIn("plain fallback 中文", result.canonical_text)
        self.assertNotIn("\x00", result.canonical_text)
        self.assertNotIn("<msg>", parse_appmsg("<msg>").canonical_text)

    def test_reference_ids_are_atomic_under_display_budget_pressure(self):
        for limits in (AppMessageLimits(max_output_bytes=256), AppMessageLimits()):
            xml = app(57, reference("quoted", sender="", server_id="12345678901234567890"),
                      title="x" * (limits.max_output_bytes - 6))
            result = parse_appmsg(xml, limits=limits)
            self.assertIsNone(result.reply_to)
            self.assertTrue(result.truncated)
            self.assertNotEqual(result.reply_to, "123456")

    def test_canonical_output_exact_boundary_and_one_byte_over(self):
        prefix_bytes = len("[链接] ".encode())
        for offset in (0, 1):
            xml = app(5, title="x" * (MAX_OUTPUT_BYTES - prefix_bytes + offset))
            result = parse_appmsg(xml)
            self.assertEqual(result.truncated, bool(offset))
            self.assertLessEqual(len(result.canonical_text.encode()), MAX_OUTPUT_BYTES)
            self.assertEqual(result.parse_status, "partial" if offset else "ok")

    def test_quote_summary_exact_boundary_and_one_byte_over(self):
        for offset in (0, 1):
            # Two-byte characters make a byte-vs-code-point mistake visible.
            text = "é" * (MAX_QUOTE_BYTES // 2) + "x" * offset
            result = parse_appmsg(app(57, reference(text)))
            self.assertEqual(result.truncated, bool(offset))
            self.assertLessEqual(len(result.quote_summary.encode()), MAX_QUOTE_BYTES)

    def test_nested_composed_display_output_has_one_aggregate_budget(self):
        inner = record([entry("x" * 1000)])
        nested = entry("", kind=17, extra=record_field(inner))
        limits = AppMessageLimits(max_output_bytes=2048)
        result = parse_appmsg(forward([nested, nested]), limits=limits)
        self.assertLessEqual(display_bytes(result), limits.max_output_bytes)
        self.assertLessEqual(len(result.canonical_text.encode()), limits.max_output_bytes)
        self.assertTrue(result.truncated)

    def test_default_xml_byte_boundary_and_no_bom_prefixed_raw_xml_fallback(self):
        shell = app(5, title="")
        padding = MAX_XML_BYTES - len(shell.encode())
        xml = app(5, title="x" * padding)
        result = parse_appmsg(xml)
        self.assertNotEqual(result.parse_status, "limit_exceeded")
        self.assertIn("output_limit", result.issues)
        result = parse_appmsg(xml + " ")
        self.assertIn("xml_byte_limit", result.issues)
        result = parse_appmsg("﻿<msg>SECRET_RAW_XML")
        self.assertEqual(result.parse_status, "invalid")
        self.assertNotIn("SECRET_RAW_XML", result.canonical_text)

    def test_inline_quote_content_and_split_feed_declaration_rejection(self):
        xml = app(57, '<refermsg><type>49</type><svrid>123</svrid><content>'
                  + app(5) + '</content></refermsg>')
        result = parse_appmsg(xml)
        self.assertEqual(result.parse_status, "ok")
        self.assertIn("[链接]", result.quote_summary)
        prefix = '<!--' + ' ' * 4082 + '-->'
        result = parse_appmsg(prefix + '<!DOCTYPE msg SYSTEM "file:///never-open">' + app(5))
        self.assertEqual(result.parse_status, "rejected")

    def test_limits_cannot_disable_hard_ceilings_or_take_booleans(self):
        for kwargs in ({"max_nodes": MAX_XML_NODES + 1}, {"max_depth": 0},
                       {"max_entries": True}, {"max_nesting": 3}, {"max_output_bytes": 255},
                       {"max_quote_bytes": 0}, {"max_xml_bytes": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                AppMessageLimits(**kwargs)
        with self.assertRaises(TypeError):
            parse_appmsg(Path("not-a-file-to-open"))
        with self.assertRaises(TypeError):
            parse_appmsg("x", limits={})


if __name__ == "__main__":
    unittest.main()
