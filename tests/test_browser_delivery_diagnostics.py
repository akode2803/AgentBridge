"""Real Chromium collector/module integration, not authenticated app/peer acceptance.

No app accounts or credentials are created. The message/read HTTP responses are
synthetic; diagnostics.js, browser DOM/fetch, and the disposable Diagnostics sink
are real. Set AGENTBRIDGE_CHROMIUM_EXECUTABLE to select a local Chromium binary.
"""

import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="requires optional Playwright")

from agentbridge.gui.diagnostics import Diagnostics  # noqa: E402


SENT_NS = "9007199254740993123"
RECEIVED_NS = "9007199254740993129"
SENT_ID = f"m-{SENT_NS}-PRIVATE_SENT"
RECEIVED_ID = f"m-{RECEIVED_NS}-PRIVATE_RECEIVED"
SENT_TRACE = "a" * 16
RECEIVED_TRACE = "b" * 16
PRIVATE_CHAT = "PRIVATE_CHAT"


def _rows(sink):
    assert sink.flush(timeout=1), "diagnostics writer did not drain within its bound"
    return [
        json.loads(line)
        for path in sorted(sink.directory.glob("events*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


@pytest.fixture
def browser_collector(tmp_path):
    sink = Diagnostics(tmp_path)
    module = (Path(__file__).resolve().parents[1] / "gui/static/js/diagnostics.js").read_bytes()
    uploads = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, data, content_type="application/json"):
            raw = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/":
                self.reply(b'<div id="transcript"></div><script type="module">'
                           b'import * as d from "/diagnostics.js";window.d=d;</script>',
                           "text/html")
            elif self.path == "/diagnostics.js":
                self.reply(module, "text/javascript")
            elif self.path == "/synthetic/incoming":
                self.reply({"type": "message", "id": RECEIVED_ID,
                            "ns": int(RECEIVED_NS), "chat_id": PRIVATE_CHAT,
                            "diagnostic_ref": RECEIVED_TRACE, "body": "PRIVATE_BODY"})
            else:
                self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                self.send_error(413)
                return
            data = json.loads(self.rfile.read(length))
            if self.path == "/api/diagnostics/events":
                uploads.append(data)
                accepted, dropped = sink.collect(data["events"])
                self.reply({"ok": True, "accepted": accepted,
                            "dropped": dropped, "max_events": 50})
            elif self.path == "/api/mesh/post":
                sink.flight_record({"event": "server_request", "phase": "request_finished",
                                    "route": self.path, "status": "ok",
                                    "request_ref": self.headers.get("X-Diagnostic-Ref")})
                self.reply({"id": SENT_ID, "ns": int(SENT_NS), "body": "PRIVATE_BODY",
                            "_diagnostics": {"trace_ref": SENT_TRACE, "chat_ref": "c" * 16}})
            elif self.path == "/api/mesh/chat_page_read":
                self.reply({"ok": True, "read_ns": data["read_ns"]})
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    server.timeout = 1
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    browser = None
    try:
        with playwright.sync_playwright() as driver:
            configured = os.environ.get("AGENTBRIDGE_CHROMIUM_EXECUTABLE")
            executable = configured or next(
                (found for name in ("chromium", "chromium-browser", "google-chrome")
                 if (found := shutil.which(name))), driver.chromium.executable_path)
            if not Path(executable).is_file():
                if configured:
                    pytest.fail("AGENTBRIDGE_CHROMIUM_EXECUTABLE is not a local executable")
                pytest.skip("no local Chromium executable available")
            # Avoid detached zygotes that the container's PID 1 may not reap.
            browser = driver.chromium.launch(executable_path=executable, headless=True,
                                             args=["--no-zygote"], timeout=8000)
            context = browser.new_context(service_workers="block")
            # All page network requests stay on this disposable loopback server.
            context.route("**/*", lambda route: route.continue_()
                          if route.request.url.startswith(base + "/") else route.abort())
            page = context.new_page()
            page.set_default_timeout(5000)
            page.goto(base)
            page.wait_for_function("!!window.d")
            page.evaluate("""() => {
                window.chat = 'PRIVATE_CHAT';
                window.transcript = document.querySelector('#transcript');
                window.addRow = (id, pending=false) => {
                    const row = document.createElement('div'); row.dataset.mid = id;
                    row.textContent = 'PRIVATE_BODY';
                    if (pending) { row.classList.add('pending-send'); row.dataset.pendingRef='PRIVATE'; }
                    transcript.append(row); return row;
                };
                window.ack = async cutoff => {
                    const response = await fetch('/api/mesh/chat_page_read', {method:'POST',
                        headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({chat_id:chat, read_ns:cutoff})});
                    d.acknowledgedDelivery(chat, (await response.json()).read_ns);
                };
            }""")
            yield SimpleNamespace(page=page, sink=sink, uploads=uploads)
            context.close()
            browser.close()
            browser = None
    finally:
        if browser is not None and browser.is_connected():
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        sink.close()
        if sink._worker is not None:
            sink._worker.join(timeout=1)
            assert not sink._worker.is_alive(), "diagnostics worker was not reaped"
        assert not thread.is_alive(), "loopback HTTP server was not reaped"


@pytest.mark.timeout(30)
def test_real_chromium_delivery_dom_upload_privacy_and_optout(browser_collector):
    rig = browser_collector
    page, sink = rig.page, rig.sink
    assert not sink.enabled
    assert page.evaluate("""() => {
        d.diagnostic('client_error', {error_type:'Error', body:'PRIVATE_BODY'});
        addRow('PRIVATE_DISABLED');
        return d.beginDiagnosticRequest('/api/mesh/post', {chat_id:chat});
    }""") is None
    page.wait_for_timeout(1150)  # Longer than the real module's one-second upload timer.
    assert rig.uploads == [] and _rows(sink) == []

    assert sink.set_enabled(True, sample_rate=1)
    with page.expect_response("**/api/diagnostics/events"):
        page.evaluate("""async () => {
            d.configureDiagnostics(true);
            const ref = d.beginDiagnosticRequest('/api/mesh/post', {chat_id:chat});
            const response = await fetch('/api/mesh/post', {method:'POST',
                headers:{'Content-Type':'application/json', 'X-Diagnostic-Ref':ref},
                body:JSON.stringify({chat_id:chat, body:'PRIVATE_BODY'})});
            window.sent = await response.json();
            d.endDiagnosticRequest(ref, '/api/mesh/post', {}, sent);
            addRow('PRIVATE_WRONG_ID'); window.sentRow = addRow(sent.id, true);
            d.canonicalDeliveryDom(chat, [sent], transcript); await ack('9007199254740993123');
            d.diagnostic('client_request', {route:'/api/state?cursor=PRIVATE_CURSOR',
                body:'PRIVATE_BODY', sql:'PRIVATE_SQL', path:'PRIVATE_PATH'});
        }""")
    rows = _rows(sink)
    assert not any(row.get("phase") in ("canonical_dom", "native_ack") for row in rows)
    assert any(row["event"] == "transcript_state" and row["rows"] == 3 for row in rows)

    with page.expect_response("**/api/diagnostics/events"):
        page.evaluate("""async () => {
            sentRow.classList.remove('pending-send'); delete sentRow.dataset.pendingRef;
            d.canonicalDeliveryDom(chat, [sent], transcript); await ack('9007199254740993122');
        }""")
    rows = _rows(sink)
    assert sum(row.get("phase") == "canonical_dom" for row in rows) == 1
    assert not any(row.get("phase") == "native_ack" for row in rows)

    with page.expect_response("**/api/diagnostics/events"):
        page.evaluate("""async () => {
            await ack('9007199254740993123');
            window.incoming = await (await fetch('/synthetic/incoming')).json();
            d.receivedDelivery(incoming); addRow(incoming.id);
            d.canonicalDeliveryDom(chat, [incoming], transcript);
            await ack('9007199254740993128');
        }""")
    rows = _rows(sink)
    assert sum(row.get("phase") == "native_ack" and row.get("flow") == "sent"
               for row in rows) == 1
    assert not any(row.get("phase") == "native_ack" and row.get("flow") == "received"
                   for row in rows)

    with page.expect_response("**/api/diagnostics/events"):
        page.evaluate("async () => { await ack('9007199254740993129'); }")
    # Also exercise server sanitization through real HTTP with forged client fields.
    page.evaluate("""async () => {
        await fetch('/api/diagnostics/events', {method:'POST',
            headers:{'Content-Type':'application/json'}, body:JSON.stringify({events:[{
                event:'delivery', phase:'browser_response', tab_ref:'dddddddddddddddd',
                monotonic_ms:7, clock_ref:'eeeeeeeeeeeeeeee', body:'PRIVATE_FORGED_BODY',
                id:'PRIVATE_FORGED_ID', sql:'PRIVATE_FORGED_SQL', path:'PRIVATE_FORGED_PATH'
            }]})});
    }""")
    rows = _rows(sink)
    for flow, ref in (("sent", SENT_TRACE), ("received", RECEIVED_TRACE)):
        for phase in ("canonical_dom", "native_ack"):
            assert sum(row.get("phase") == phase and row.get("flow") == flow
                       and row.get("trace_ref") == ref for row in rows) == 1
    browser_rows = [row for row in rows if row["origin"] == "browser"]
    server_rows = [row for row in rows if row["origin"] == "server"]
    assert browser_rows and server_rows
    assert all(row["clock_ref"] == row["tab_ref"] != sink.clock_ref for row in browser_rows)
    assert all(row["clock_ref"] == sink.clock_ref for row in server_rows)
    assert any(row["monotonic_ms"] == 7 for row in browser_rows)
    assert all(isinstance(row["monotonic_ms"], (int, float)) for row in rows)
    assert "PRIVATE" not in json.dumps(rows)
    assert "PRIVATE" not in json.dumps(rig.uploads[:-1]), "module uploads leaked private fields"

    before = len(rig.uploads)
    assert sink.set_enabled(False)
    page.evaluate("""async () => {
        const late = d.beginDiagnosticRequest('/api/mesh/post', {chat_id:chat});
        d.diagnostic('client_error', {error_type:'Error'}); d.configureDiagnostics(false);
        d.endDiagnosticRequest(late, '/api/mesh/post', {}, sent);
        addRow('PRIVATE_LATE'); d.receivedDelivery(incoming);
        d.canonicalDeliveryDom(chat, [sent, incoming], transcript);
        await ack('9007199254740993129');
        window.dispatchEvent(new ErrorEvent('error', {message:'PRIVATE_ERROR'}));
    }""")
    page.wait_for_timeout(1150)
    assert len(rig.uploads) == before, "disabled module uploaded queued/late events"
    assert _rows(sink) == rows, "disabled sink admitted late observations"
