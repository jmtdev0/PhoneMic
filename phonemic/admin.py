"""Pairing page for the PC owner: QR code, paired phones and revoke. Reachable from this PC only.

It listens on 127.0.0.1 over plain HTTP, so there is no certificate warning, and it refuses any request whose
peer is not loopback or whose Host header is not a loopback name (DNS rebinding). Changing requests also need a custom
header, which a page on another website cannot add without a CORS preflight that this server never grants.
"""

from __future__ import annotations

import html
import urllib.parse

import segno
from aiohttp import web

from .certs import format_pin
from .server import STATE, ServerInfo, ServerState, is_loopback_address, revoke_device, set_permissions

ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
CSRF_HEADER = "X-PhoneMic"


def pairing_link(token: str, info: ServerInfo) -> str:
    """The URL encoded in the QR code; the phone's camera app opens it with PhoneMic."""
    host = f"{info.ips[0] if info.ips else 'localhost'}:{info.port}"
    query = urllib.parse.urlencode({"v": 1, "t": token, "p": info.pin_hex, "h": host, "n": info.name})
    return f"phonemic://pair?{query}"


def _qr_svg(link: str) -> str:
    # omitsize: a viewBox instead of fixed width/height, so the page CSS can scale the code without cropping it.
    return segno.make(link, error="m").svg_inline(scale=5, border=2, dark="#111", light="#fff", omitsize=True)


def _devices_rows(state: ServerState) -> str:
    rows = []
    for device in state.registry.list():
        seen = f"{html.escape(device.last_seen[:16].replace('T', ' '))} UTC from {html.escape(device.last_address or '?')}" if device.last_seen else "never"
        rows.append(
            "<tr>"
            f"<td>{html.escape(device.name)}</td>"
            f"<td>{html.escape(device.created_at[:10])}</td>"
            f"<td>{seen}</td>"
            f'<td><input type="checkbox" class="perm" data-id="{html.escape(device.id)}" data-kind="dictation"'
            f'{" checked" if device.permissions["dictation"] else ""} aria-label="Allow dictation"></td>'
            f'<td><input type="checkbox" class="perm" data-id="{html.escape(device.id)}" data-kind="send"'
            f'{" checked" if device.permissions["send"] else ""} aria-label="Allow sending"></td>'
            f'<td><button class="revoke" data-id="{html.escape(device.id)}">Revoke</button></td>'
            "</tr>"
        )
    if not rows:
        return '<tr><td colspan="6" class="muted">No phone is paired yet.</td></tr>'
    return "".join(rows)


def render_page(state: ServerState, token: str) -> str:
    info = state.info
    link = pairing_link(token, info)
    address = f"{info.ips[0] if info.ips else 'localhost'}:{info.port}"
    ttl = int(state.enrollment.ttl_seconds)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PhoneMic pairing</title>
<style>
  :root {{ --bg:#14161a; --card:#1f2329; --text:#e8eaed; --muted:#9aa0a6; --accent:#4f9dff; --bad:#ff6b6b; }}
  @media (prefers-color-scheme: light) {{ :root {{ --bg:#f4f5f7; --card:#fff; --text:#1c1e21; --muted:#5f6368; --accent:#1a73e8; --bad:#c5221f; }} }}
  body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--text); font:16px/1.45 system-ui,sans-serif; }}
  main {{ max-width:680px; margin:0 auto; display:grid; gap:16px; }}
  section {{ background:var(--card); border-radius:14px; padding:20px; }}
  h1 {{ margin:0 0 4px; font-size:1.4rem; }} h2 {{ margin:0 0 10px; font-size:1.05rem; }}
  .muted {{ color:var(--muted); }} code {{ word-break:break-all; font-size:.85rem; }}
  .qr {{ display:flex; gap:20px; align-items:center; flex-wrap:wrap; }}
  .qr svg {{ width:220px; height:220px; border-radius:8px; background:#fff; }}
  table {{ width:100%; border-collapse:collapse; font-size:.92rem; }}
  td, th {{ text-align:left; padding:6px 8px 6px 0; border-bottom:1px solid rgba(128,128,128,.25); }}
  button {{ padding:6px 12px; border:0; border-radius:8px; background:var(--bad); color:#fff; cursor:pointer; }}
</style></head>
<body><main>
<section>
  <h1>PhoneMic</h1>
  <p class="muted">Pair a phone once and it connects by itself from then on.</p>
</section>
<section>
  <h2>Pair a new phone</h2>
  <div class="qr">
    {_qr_svg(link)}
    <div>
      <p>1. Install the Android app (open <code>https://{html.escape(address)}/app.apk</code> on the phone).<br>
         2. Scan this code with the phone's <b>camera app</b>; it opens PhoneMic.<br>
         3. Confirm the pairing in the app.</p>
      <p class="muted">The code works once and expires in <b id="left">{ttl // 60}:{ttl % 60:02d}</b>.
         Reloading this page makes a new one and cancels this one.</p>
      <details><summary class="muted">Can't scan the code?</summary>
        <p class="muted">Send this link to the phone and tap it:<br><code>{html.escape(link)}</code></p></details>
    </div>
  </div>
</section>
<section>
  <h2>Paired phones</h2>
  <table><thead><tr><th>Name</th><th>Paired</th><th>Last connection</th><th>Dictate</th><th>Send</th><th></th></tr></thead>
  <tbody id="devices">{_devices_rows(state)}</tbody></table>
  <p class="muted">Dictate and Send let a phone press Claude Desktop's microphone and send buttons on this PC from the
     app's buttons. Both are <b>off</b> until you tick them, and Send needs Dictate. A phone can only send after it
     has dictated something itself.</p>
</section>
<section>
  <h2>This PC</h2>
  <p class="muted">PC name: <code>{html.escape(info.name)}</code><br>
     Phone address: <code>{html.escape(address)}</code><br>
     Browser page code (for the web page only): <code>{html.escape(state.guard.code)}</code><br>
     Server key pin (the app shows the same value):<br><code>{html.escape(format_pin(info.pin_hex))}</code></p>
</section>
</main>
<script>
const header = {{ '{CSRF_HEADER}': '1' }};
let seconds = {ttl};
setInterval(() => {{
  seconds -= 1;
  if (seconds <= 0) {{ location.reload(); return; }}
  document.getElementById('left').textContent = Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
}}, 1000);
document.querySelectorAll('.revoke').forEach((button) => button.addEventListener('click', async () => {{
  if (!confirm('Revoke this phone? It will have to be paired again.')) return;
  await fetch('/api/devices/' + button.dataset.id + '/revoke', {{ method: 'POST', headers: header }});
  location.reload();
}}));
document.querySelectorAll('.perm').forEach((box) => box.addEventListener('change', async () => {{
  const row = box.closest('tr');
  const dictation = row.querySelector('[data-kind=dictation]');
  const send = row.querySelector('[data-kind=send]');
  if (send.checked) dictation.checked = true;  // sending needs dictating
  const response = await fetch('/api/devices/' + box.dataset.id + '/permissions', {{
    method: 'POST', headers: {{ ...header, 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ dictation: dictation.checked, send: send.checked }}),
  }});
  if (!response.ok) location.reload();
}}));
let known = null;
setInterval(async () => {{
  const ids = (await (await fetch('/api/devices')).json()).map((d) => d.id).join(',');
  if (known !== null && ids !== known) location.reload();
  known = ids;
}}, 2500);
</script></body></html>"""


@web.middleware
async def _loopback_only(request: web.Request, handler):
    if not is_loopback_address(request.remote):
        raise web.HTTPForbidden(text="This page is only available on the PC itself.")
    hostname = urllib.parse.urlsplit("//" + request.host).hostname
    if hostname not in ALLOWED_HOSTS:
        raise web.HTTPForbidden(text="Unexpected Host header.")
    if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "1":
        raise web.HTTPForbidden(text="Missing request header.")
    return await handler(request)


async def _page(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    token = state.enrollment.create()
    return web.Response(
        text=render_page(state, token),
        content_type="text/html",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"},
    )


async def _devices(request: web.Request) -> web.StreamResponse:
    return web.json_response([{"id": d.id, "name": d.name} for d in request.app[STATE].registry.list()])


async def _revoke(request: web.Request) -> web.StreamResponse:
    if not await revoke_device(request.app[STATE], request.match_info["device_id"]):
        raise web.HTTPNotFound(text="Unknown device.")
    return web.json_response({"revoked": True})


async def _permissions(request: web.Request) -> web.StreamResponse:
    try:
        body = await request.json()
        dictation, send = body["dictation"], body["send"]
        if not isinstance(dictation, bool) or not isinstance(send, bool):
            raise TypeError("booleans expected")
    except (ValueError, KeyError, TypeError):
        raise web.HTTPBadRequest(text='Expected {"dictation": true|false, "send": true|false}.')
    result = await set_permissions(request.app[STATE], request.match_info["device_id"], dictation=dictation, send=send)
    if result is None:
        raise web.HTTPNotFound(text="Unknown device.")
    return web.json_response(result)


def create_admin_app(phone_app: web.Application) -> web.Application:
    """The pairing page, sharing the phone-facing app's state (devices, tokens, active session)."""
    app = web.Application(middlewares=[_loopback_only])
    app[STATE] = phone_app[STATE]
    app.router.add_get("/", _page)
    app.router.add_get("/api/devices", _devices)
    app.router.add_post("/api/devices/{device_id}/revoke", _revoke)
    app.router.add_post("/api/devices/{device_id}/permissions", _permissions)
    return app
