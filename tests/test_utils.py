from diagcompleto import report, startup
from diagcompleto.health import CRIT, OK, HealthItem, HealthReport
from diagcompleto.utils import as_list, decode_output, format_bytes


def test_format_bytes():
    assert format_bytes(None) == "—"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1536) == "1,5 KB"
    assert format_bytes(3 * 1024 ** 3) == "3,0 GB"


def test_as_list():
    assert as_list(None) == []
    assert as_list({"a": 1}) == [{"a": 1}]
    assert as_list([1, 2]) == [1, 2]


def test_decode_utf16_output():
    assert decode_output("Verificação".encode("utf-16-le")) == "Verificação"


def test_startup_lists_entries():
    entries = startup.list_entries()
    assert all(e.name and e.source for e in entries)


def test_report_escapes_html(tmp_path):
    from datetime import datetime
    rep = HealthReport(items=[HealthItem("Disco", "<script>", CRIT, "a & b"), HealthItem("X", "y", OK, "")],
                       score=70, summary={"Sistema": "Windows"}, collected_at=datetime.now(), admin=False)
    path = report.save_report(rep, tmp_path)
    html = path.read_text(encoding="utf-8")
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "a &amp; b" in html
