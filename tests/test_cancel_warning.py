"""Tests for the Spaghetti WARNING safety feature: Pause + Cancel print inside the WARNING block, the "Open in Fabrica" link in the
alert message, the tappable fleet banner and warned card, the phone's sticky Pause / Cancel bar and the 5-inch Cancel frame.

    python3 tests/test_cancel_warning.py

Python part: the alert message (events.py) and what the Telegram / ntfy channels send. Static part: dashboard.html read as text.
Browser part: the real page in headless Chrome / Edge with a stubbed `fetch` (skipped when no browser is found); nothing contacts a printer
or the network. Nothing in the feature pauses or cancels by itself: every request below is made by a click in the driver. Names are generic.
"""
import json
import os
import re
import sys
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import notify  # noqa: E402
import test_finished_message as tfm  # noqa: E402
from test_layout_5in import run_q  # noqa: E402
from test_notify import TG_TOKEN, FakeHTTP, JPEG, parse_multipart, tg_ok  # noqa: E402
from test_parity_layouts import PRE, SCREEN_DRIVER, WIDE  # noqa: E402

with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()

HUB = tfm.HUB
LINK = tfm.LINK                      # <hub>/dashboard?printer=Printer-A


# ============================================================================================================ the alert message

class AlertMessage(tfm.Base):
    """spaghetti_suspected carries the same "Open in Fabrica" line as the finished message (the same _hub_link)."""

    def spag(self, level="warning", name="Printer-A", score=0.71):
        return self.w.spaghetti_suspected(name, score, level=level)

    def test_the_warning_body_ends_with_the_open_in_fabrica_link(self):
        self.settings.update({"notifications.hub_url": HUB})
        ev = self.spag("warning")
        self.assertEqual(ev.kind, "spaghetti_suspected")
        self.assertEqual(ev.body.splitlines()[-1], f"Open in Fabrica: {LINK}")
        self.assertEqual(ev.data["link"], LINK)

    def test_the_failure_body_has_it_too_and_the_old_words_are_unchanged(self):
        self.settings.update({"notifications.hub_url": HUB})
        ev = self.spag("failure")
        self.assertEqual(ev.body.splitlines()[-1], f"Open in Fabrica: {LINK}")
        self.assertEqual(ev.body.splitlines()[0], "The failure detector is 71% sure this print has gone wrong. Check the camera and stop the print if needed.")
        self.assertEqual((ev.level, ev.title), ("failure", "Printer-A: possible print FAILURE"))
        w = self.spag("warning")
        self.assertEqual(w.body.splitlines()[0], "The failure detector is 71% sure this print has gone wrong. Check the camera.")
        self.assertEqual((w.level, w.title), ("warning", "Printer-A: possible print problem (WARNING)"))

    def test_it_is_the_very_link_the_finished_message_uses(self):
        self.settings.update({"notifications.hub_url": HUB + "/"})
        fin = self.run_print()
        ev = self.spag("warning")
        self.assertEqual(ev.body.splitlines()[-1], fin.body.splitlines()[-1])
        self.assertEqual(ev.data["link"], fin.data["link"])
        self.assertEqual(ev.data["link"], self.w._hub_link("Printer-A"))

    def test_the_link_opens_that_printer(self):
        self.settings.update({"notifications.hub_url": "https://hub.example.net/dashboard"})
        u = urllib.parse.urlparse(self.spag().data["link"])
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "hub.example.net", "/dashboard"))
        self.assertEqual(urllib.parse.parse_qs(u.query), {"printer": ["Printer-A"]})

    def test_the_printer_name_is_url_encoded(self):
        self.settings.update({"notifications.hub_url": HUB})
        for name, want in (("Shop Printer", "Shop%20Printer"), ("A&B #1", "A%26B%20%231"), ("Zäh/Ü", "Z%C3%A4h%2F%C3%9C")):
            self.assertTrue(self.spag("warning", name).body.endswith("?printer=" + want), name)

    def test_the_telegram_twin_has_a_real_link_and_escaped_words(self):
        self.settings.update({"notifications.hub_url": HUB})
        ev = self.spag("warning", "A&B")
        self.assertTrue(ev.html.startswith("A&amp;B: possible print problem (WARNING)\n"))
        self.assertEqual(ev.html.splitlines()[-1], '\U0001F517 <a href="%s">Open in Fabrica</a>' % (HUB + "/dashboard?printer=A%26B"))
        self.assertNotIn("A&B", ev.html.replace("A&amp;B", ""))

    def test_no_hub_url_means_no_link_and_the_message_is_what_it_was(self):
        ev = self.spag("warning")
        self.assertNotIn("Fabrica", ev.body)
        self.assertNotIn("http", ev.body)
        self.assertIsNone(ev.html)
        self.assertIsNone(ev.data["link"])
        self.assertEqual(len(ev.body.splitlines()), 1)

    def test_dedupe_level_and_the_picture_are_untouched(self):
        self.settings.update({"notifications.hub_url": HUB})
        a, b = self.spag("warning"), self.spag("failure")
        self.assertEqual((a.dedupe_key, b.dedupe_key), ("|warning", "|failure"))       # a WARNING followed by a FAILURE is never swallowed as a repeat
        self.assertEqual(self.w.spaghetti_suspected("Printer-A", 0.9, JPEG).get_image(), JPEG)
        json.dumps(a.to_dict())

    def test_plain_text_has_no_emoji(self):
        self.settings.update({"notifications.hub_url": HUB})
        ev = self.spag("failure")
        self.assertTrue(all(ord(c) < 0x2000 for c in ev.title + ev.body), ev.body)


class AlertDelivery(tfm.Base):
    def setUp(self):
        super().setUp()
        self.srv = FakeHTTP()
        self.addCleanup(self.srv.close)
        self.srv.respond = tg_ok
        self.settings.update({"telegram": {"enabled": True, "chat_id": "-100123", "bot_token": TG_TOKEN}, "notifications.hub_url": HUB})
        self.n = notify.Notifier(self.settings, clock=lambda: tfm.START, timeout=5, retry_delays=(),
                                 channel_options={"telegram": {"api_base": self.srv.url}})
        self.addCleanup(self.n.close)

    def test_telegram_sends_the_picture_with_the_link_as_its_last_line(self):
        ev = self.w.spaghetti_suspected("Printer-A", 0.71, level="warning")
        self.assertTrue(self.n.notify(ev)["sent"])
        self.assertEqual([r["path"].rsplit("/", 1)[1] for r in self.srv.requests], ["sendPhoto"])
        parts = parse_multipart(self.srv.requests[0])
        self.assertEqual(parts["parse_mode"]["data"], b"HTML")
        self.assertEqual(parts["caption"]["data"].decode(), ev.html)
        self.assertTrue(parts["caption"]["data"].decode().endswith(f'\U0001F517 <a href="{LINK}">Open in Fabrica</a>'))

    def test_other_channels_get_the_plain_line(self):
        self.settings.update({"telegram": {"enabled": False}, "ntfy": {"enabled": True, "server_url": self.srv.url, "topic": "hub"}})
        self.w = self.watcher()
        self.w.fetch = lambda n: None
        ev = self.w.spaghetti_suspected("Printer-A", 0.9, level="failure")
        self.assertTrue(self.n.notify(ev)["sent"])
        text = self.srv.requests[0]["body"].decode()
        self.assertEqual(text, ev.body)
        self.assertIn(f"Open in Fabrica: {LINK}", text)
        self.assertNotIn("<a ", text)


# ============================================================================================================ the page, as text

def block(start, end):
    i = HTML.index(start)
    return HTML[i:HTML.index(end, i)]


SPAG_BLOCK = block('<div id="p-spag"', '<!-- printer controls')
TBAR_BLOCK = block('<div class="row tbar">', '<div class="hubdown"')


class MarkupTests(unittest.TestCase):
    def test_the_warning_block_holds_pause_and_cancel_print_wired_to_the_existing_actions(self):
        act = block('<div id="sp-act"', '</div></div>')
        self.assertEqual(len(re.findall(r"<button\b", act)), 2)
        self.assertIn('<button type="button" id="sp-pause" data-ctl onclick="act(\'pause\')">', act)           # the toolbar's own Pause call
        self.assertIn('<button type="button" id="sp-cancel" data-ctl class="danger" onclick="askCancel()">', act)   # the toolbar's own Cancel call: the ONE confirm
        self.assertRegex(act, r"</svg>Pause</button>")
        self.assertRegex(act, r"</svg>Cancel print</button>")                                                   # a word and a shape, not a colour
        self.assertIn("#i-pause", act)
        self.assertIn("#i-x", act)
        self.assertIn('hidden', act.split(">", 1)[0])                                                           # hidden until the block is in a WARNING
        self.assertIn("data-ctl", act.split(">", 1)[0])                                                         # view-only sign-ins never see them
        self.assertIn(act, SPAG_BLOCK)

    def test_the_toolbar_still_has_its_own_pause_and_cancel(self):
        self.assertIn("""<button data-ctl id="p-pause" onclick="act('pause')">Pause</button>""", TBAR_BLOCK)
        self.assertIn("""<button data-ctl class="danger" onclick="askCancel()" id="p-cancel">Cancel</button>""", TBAR_BLOCK)

    def test_there_is_one_cancel_request_and_one_confirm(self):
        self.assertEqual(HTML.count("action:'cancel'"), 1)                       # doCancel, nowhere else
        self.assertEqual(HTML.count("function askCancel("), 1)
        self.assertEqual(HTML.count("function doCancel("), 1)
        self.assertEqual(len(re.findall(r'onclick="doCancel\(\)"', HTML)), 1)    # only the confirm's own button
        self.assertNotIn("act('cancel'", HTML)
        # every Cancel button of the feature goes to askCancel (the confirm), never to the request
        self.assertIn("cardCancel: b => askCancel(b.dataset.name)", HTML)
        for m in re.finditer(r'id="(?:sp|stk)-cancel"[^>]*', HTML):
            self.assertIn('onclick="askCancel()"', m.group(0))
        self.assertIn('data-act="cardCancel"', HTML)
        self.assertNotIn('data-act="cardCancel" data-name="" onclick', HTML)
        self.assertIn("Keep printing", block("function askCancel(", "async function doCancel"))
        self.assertIn("CANCEL PRINT", block("function askCancel(", "async function doCancel"))

    def test_pause_is_one_tap_and_only_while_printing(self):
        self.assertIn("$('sp-pause').hidden = st !== 'RUNNING'", HTML)           # the toolbar's rule: (s.state==='RUNNING')
        self.assertIn("$('p-pause').style.display  = (s.state==='RUNNING')", HTML)
        self.assertIn("$('stk-pause').style.display = (s.state==='RUNNING')", HTML)
        self.assertRegex(block("function spQuick(", "\n}\n"), r"state === 'RUNNING' \? '<button type=\"button\" class=\"spq-p\"")
        self.assertIn("cardPause:  b => act('pause', b.dataset.name)", HTML)
        self.assertIn("const spAct = info.k === 'alert' && BUSY_STATES.includes(st);", HTML)       # no buttons for an idle printer
        self.assertNotIn("auto_pause: True", HTML)

    def test_nothing_pauses_or_cancels_without_a_tap(self):
        for fn in ("spQuick", "spagBanner", "stickSync", "spShow"):
            body = block("function " + fn + "(", "\n}\n" if fn != "spShow" else "\n")
            self.assertNotRegex(body, r"\bact\(|askCancel\(|doCancel\(|cancel_print|pause_print", fn)    # no automatic call; only markup that a click reaches
        self.assertNotIn("setTimeout(() => act(", HTML)
        self.assertNotIn("setInterval(() => act(", HTML)

    def test_the_fleet_banner_and_card_markup(self):
        self.assertIn("el.setAttribute('data-act', 'openPrinter'); el.setAttribute('data-name', first)", HTML)
        self.assertIn('data-act="openPrinter" data-name="\' + esc(p.printer) + \'" title="Open', HTML)         # the word on the card
        self.assertEqual(HTML.count("const quick = spag.k === 'alert' && (k === 'printing' || k === 'paused');"), 1)
        self.assertIn("spag.k === 'alert' && (k === 'printing' || k === 'paused') ? spQuick(p.printer, p.state)", HTML)
        self.assertIn('data-ctl role="group" aria-label="Pause or cancel the print on', block("function spQuick(", "\n}\n"))

    def test_the_5in_strip_cancel_has_a_heavy_frame_and_the_word(self):
        self.assertIn("html.lay-5in #p-act button.danger { border:3px solid var(--txt); font-weight:800; }", HTML)
        self.assertIn("html.lay-5in.th-bridge #p-act button.danger { border:4px solid var(--txt); }", HTML)
        self.assertRegex(HTML, r"tail\.push\('<button class=\"danger\" onclick=\"askCancel\(\)\">' \+ IC\('x'\) \+ 'Cancel&hellip;</button>'\)")

    def test_the_phone_bar_is_hidden_unless_printing_scrolled_and_on_the_phone_layout(self):
        self.assertIn(".pstick { display:none; }", HTML)
        rule = "html:not(.lay-5in) #printer.stk .pstick { display:flex; }"
        self.assertEqual(HTML.count(rule), 1)
        k = HTML.index(rule)
        media = HTML[HTML.rindex("@media", 0, k):k]
        self.assertTrue(media.startswith("@media (max-width:760px), (orientation:landscape) and (max-height:500px) and (max-width:932px)"), media[:120])   # the phone layout's own query
        self.assertNotIn("\n}\n", media)                                                                                     # still inside that block
        self.assertIn("position:fixed", HTML[HTML.index("html:not(.lay-5in) .pstick { position:fixed"):][:80])
        self.assertIn("on = c.height > 0 && c.bottom < 4;", HTML)                                                             # only once the toolbar row has scrolled out of sight
        self.assertIn("BUSY_STATES.includes(FS.pstate)", block("function stickSync(", "\n}\n"))                                # only while a print exists


# ============================================================================================================ the page, in a browser

PH_STUBS = r"""
window.__stubs.push((u, m) => { const p = u.split('?')[0]; if(m === 'POST' && /^\/api\/printer\/[^/]+\/action$/.test(p)) return J({ok: true, message: 'ok'}); return null; });
const posts = () => window.__calls.filter(c => /^POST \/api\/printer\/[^/]+\/action /.test(c));
const cut = e => e.scrollWidth > e.clientWidth + 1 || e.scrollHeight > e.clientHeight + 1;
const mo = () => document.getElementById('modal').classList.contains('on');
const dlg = () => ({open: mo(), title: (document.querySelector('#mbody h2') || {textContent: ''}).textContent, text: document.getElementById('mbody').textContent.replace(/\s+/g, ' ').trim(),
  btns: [...document.querySelectorAll('#mbody button')].map(b => b.textContent.trim())});
const clickBtn = re => { const b = [...document.querySelectorAll('#mbody button')].find(x => re.test(x.textContent)); b.click(); };
const WARN = {state: 'warning', text: 'WARNING - possible spaghetti · score 0.71', score: 0.71, learn: false, alerts: 1};
const warnSnap = () => {
  const blk = document.getElementById('p-spag'), act = document.getElementById('sp-act'), pb = document.getElementById('sp-pause'), cb = document.getElementById('sp-cancel'), pr = document.getElementById('printer');
  return {k: blk.dataset.k, act: vis(act), pause: vis(pb), cancel: vis(cb), blk: r(blk), actR: vis(act) ? r(act) : null, pr: vis(pb) ? r(pb) : null, cr: vis(cb) ? r(cb) : null,
    cut: [pb, cb].filter(vis).filter(cut).map(e => e.id), words: [pb, cb].filter(vis).map(e => e.textContent.replace(/\s+/g, ' ').trim()),
    vw: document.documentElement.clientWidth, vh: innerHeight, docW: document.documentElement.scrollWidth, docH: document.documentElement.scrollHeight, spw: pr.classList.contains('spw'), blkVis: vis(blk),
    cam: r(document.getElementById('cam')), scrollY: Math.round(scrollY), tbar: r(document.querySelector('#printer .tbar')), blkCut: cut(document.querySelector('#p-spag .t .l')) ,
    stick: vis(document.getElementById('p-stick')), stk: pr.classList.contains('stk'), tilesVis: vis(document.getElementById('p-tiles')),
    stripCancel: (() => { const c = [...document.querySelectorAll('#p-act button.danger')][0]; return c ? {w: getComputedStyle(c).borderTopWidth, text: c.textContent.replace(/\s+/g, ' ').trim()} : null; })()};
};
"""

SCREEN_PRO = SCREEN_DRIVER.split("await sleep(300);\nR.a =")[0] + PH_STUBS

WARN_FLOW = r"""
await sleep(300);
await open('Printer-A'); R.run = warnSnap();
DETLINE['Printer-A'] = WARN; await pview(); await sleep(250); R.warn = warnSnap();
window.__calls.length = 0; document.getElementById('sp-pause').click(); await sleep(300);          // Pause: one tap, no confirm
R.pauseCalls = posts(); R.pauseModal = mo();
window.__calls.length = 0; document.getElementById('sp-cancel').click(); await sleep(150);          // Cancel: the ONE confirm
R.confirm = dlg(); R.confirmCalls = posts().length; R.dialogs = document.querySelectorAll('#mbody h2').length;
clickBtn(/Keep printing/); await sleep(100); R.keep = {open: mo(), calls: posts().length};
document.getElementById('sp-cancel').click(); await sleep(100); clickBtn(/CANCEL PRINT/); await sleep(400);
R.cancelCalls = posts(); R.cancelClosed = !mo();
FLEET[0] = Object.assign({}, FLEET[0], {state: 'PAUSE'}); await pview(); await sleep(250); R.paused = warnSnap();      // paused: no Pause, Cancel stays
FLEET[0] = Object.assign({}, FLEET[0], {state: 'IDLE'}); await pview(); await sleep(250); R.idle = warnSnap();         // not printing: no buttons at all
FLEET[0] = Object.assign({}, FLEET[0], {state: 'RUNNING'}); await pview(); await sleep(250); R.back = warnSnap();
delete DETLINE['Printer-A']; await pview(); await sleep(250); R.cleared = warnSnap();                                  // the warning clears: the block is as before
DETLINE['Printer-A'] = WARN; await pview(); await sleep(250);
document.body.classList.add('viewonly'); await sleep(50); R.viewonly = warnSnap(); document.body.classList.remove('viewonly');
"""


class WarningBlockTests(unittest.TestCase):
    R = {}

    def go(self, key, query, size, wide=False):
        k = (key, query, size, wide)
        if k not in WarningBlockTests.R:
            drv = SCREEN_PRO + (WIDE if wide else "") + WARN_FLOW
            WarningBlockTests.R[k] = run_q(self, drv, query, size, 240000)
        R = WarningBlockTests.R[k]
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        return R

    LAYOUTS = (("kiosk", "?kiosk=1", (1024, 600)), ("5in", "?layout=5in", (800, 480)), ("bridge", "?layout=5in&theme=bridge", (800, 480)),
               ("desktop", "?view=desktop", (1440, 900)), ("phone", "", (390, 844)))

    def check_warning(self, name, R, min_h):
        w = R["warn"]
        self.assertEqual((w["k"], w["act"], w["pause"], w["cancel"], w["spw"]), ("alert", True, True, True, True), name)
        self.assertEqual(w["words"], ["Pause", "Cancel print"], name)                                  # words on both buttons
        self.assertLessEqual(abs(w["pr"]["t"] - w["cr"]["t"]), 2, (name, w["pr"], w["cr"]))             # ONE row
        self.assertLessEqual(w["pr"]["r"], w["cr"]["l"] + 1, name)
        self.assertGreaterEqual(min(w["pr"]["h"], w["cr"]["h"]), min_h, name)                           # big buttons
        for b in (w["pr"], w["cr"]):
            self.assertGreaterEqual(b["l"], w["blk"]["l"], name)
            self.assertLessEqual(b["r"], w["blk"]["r"], name)                                          # inside the block
            self.assertGreaterEqual(b["t"], w["blk"]["t"], name)
            self.assertLessEqual(b["b"], w["blk"]["b"], name)
        self.assertEqual(w["cut"], [], name)                                                           # no word cut off
        self.assertLessEqual(w["blk"]["b"], w["vh"], name)                                             # on screen, nothing pushed off
        self.assertGreaterEqual(w["blk"]["l"], 0, name)
        self.assertLessEqual(w["blk"]["r"], w["vw"], name)
        self.assertEqual(w["docW"], w["vw"], name)                                                     # no sideways scroll

    def test_every_layout_shows_both_buttons_on_one_row_inside_the_warning(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            self.check_warning(name, R, 44)
            self.assertFalse(R["run"]["act"], name)                                                    # a calm print: no buttons, no extra band
            self.assertFalse(R["run"]["spw"], name)

    def test_nothing_grows_when_nothing_is_wrong(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            self.assertLessEqual(R["run"]["blk"]["h"], 80, name)                                       # the old one-row block (a phone's is 64, the desktop view's 76)
            self.assertEqual(R["cleared"]["blk"]["h"], R["run"]["blk"]["h"], name)                      # and it goes back when the warning clears
            self.assertFalse(R["cleared"]["act"], name)
            self.assertGreater(R["warn"]["blk"]["h"], R["run"]["blk"]["h"], name)

    def test_wide_fallback_fonts_cut_nothing_on_the_10_inch_and_5_inch_screens(self):
        for name, query, size in self.LAYOUTS[:3]:
            R = self.go(name, query, size, wide=True)
            self.check_warning(name + "-wide", R, 44)
            self.assertFalse(R["warn"]["blkCut"], name)

    def test_pause_is_one_tap_with_no_confirm_and_uses_the_existing_endpoint(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            self.assertEqual(R["pauseCalls"], ['POST /api/printer/Printer-A/action {"action":"pause"}'], name)
            self.assertFalse(R["pauseModal"], name)

    def test_cancel_asks_once_and_only_the_confirm_sends_it(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            c = R["confirm"]
            self.assertTrue(c["open"], name)
            self.assertEqual(c["title"], "Cancel the print on Printer-A?", name)
            self.assertEqual(c["btns"], ["Keep printing", "CANCEL PRINT"], name)
            self.assertIn("Bracket x12 for the workshop shelf.gcode.3mf", c["text"], name)
            self.assertEqual((R["dialogs"], R["confirmCalls"]), (1, 0), name)                           # one dialog; nothing sent by opening it
            self.assertEqual(R["keep"], {"open": False, "calls": 0}, name)                              # "Keep printing" sends nothing
            self.assertEqual(R["cancelCalls"], ['POST /api/printer/Printer-A/action {"action":"cancel"}'], name)
            self.assertTrue(R["cancelClosed"], name)

    def test_pause_is_hidden_when_the_print_is_not_running(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            p, i = R["paused"], R["idle"]
            self.assertEqual((p["act"], p["pause"], p["cancel"]), (True, False, True), name)           # paused: Cancel only (Resume is the toolbar's)
            self.assertEqual(p["words"], ["Cancel print"], name)
            self.assertEqual(i["k"], "alert", name)
            self.assertEqual((i["act"], i["pause"], i["cancel"], i["spw"]), (False, False, False, False), name)    # not printing: nothing to pause or cancel
            self.assertEqual((R["back"]["act"], R["back"]["pause"], R["back"]["cancel"]), (True, True, True), name)

    def test_a_view_only_sign_in_never_sees_them(self):
        for name, query, size in self.LAYOUTS:
            v = self.go(name, query, size)["viewonly"]
            self.assertEqual((v["pause"], v["cancel"]), (False, False), name)

    def test_the_5in_screen_gives_the_tiles_up_while_it_lasts_and_the_strip_cancel_is_framed(self):
        for name in ("5in", "bridge"):
            R = self.go(name, *[x for x in self.LAYOUTS if x[0] == name][0][1:])
            self.assertFalse(R["warn"]["tilesVis"], name)
            self.assertTrue(R["run"]["tilesVis"], name)
            self.assertTrue(R["cleared"]["tilesVis"], name)
            self.assertEqual(R["warn"]["stripCancel"]["w"], "4px" if name == "bridge" else "3px", name)  # heavy frame, plus the word
            self.assertIn("Cancel", R["warn"]["stripCancel"]["text"], name)
            self.assertEqual(R["warn"]["docH"], 480, name)                                               # the board never scrolls
        self.assertEqual(self.go("kiosk", "?kiosk=1", (1024, 600))["warn"]["docH"], 600)


# the deep link: the alert's "Open in Fabrica" link lands on the printer with the WARNING block first
DEEP = SCREEN_PRO + r"""
DETLINE['Printer-A'] = WARN;
await sleep(300); await fleet(); await sleep(900);       // (the page's own first poll ran before the stubs existed: this is the first answer)
R.dl = Object.assign({cur: CUR, on: document.getElementById('printer').classList.contains('on')}, warnSnap());
window.scrollTo(0, 600); await sleep(100); R.scrolled = Math.round(scrollY); spShow(); await sleep(50); R.afterShow = Math.round(scrollY);
"""
DEEP_CALM = SCREEN_PRO + r"""
await sleep(300); await fleet(); await sleep(900);
R.dl = Object.assign({cur: CUR, on: document.getElementById('printer').classList.contains('on')}, warnSnap());
"""


class DeepLinkTests(unittest.TestCase):
    R = {}

    def go(self, key, drv, query, size):
        k = (key, query, size)
        if k not in DeepLinkTests.R:
            DeepLinkTests.R[k] = run_q(self, drv, query, size, 240000)
        R = DeepLinkTests.R[k]
        self.assertNotIn("error", R, R.get("error"))
        return R

    def test_the_phone_lands_on_the_printer_with_the_warning_right_under_the_toolbar(self):
        d = self.go("phone", DEEP, "?printer=Printer-A", (390, 844))["dl"]
        self.assertEqual((d["cur"], d["on"], d["scrollY"]), ("Printer-A", True, 0))
        self.assertTrue(d["act"] and d["pause"] and d["cancel"])
        self.assertLessEqual(d["blk"]["t"], d["tbar"]["b"] + 24)                       # right under the toolbar
        self.assertLess(d["blk"]["b"], d["cam"]["t"])                                  # above the camera
        self.assertLessEqual(d["cr"]["b"], d["vh"])                                    # both buttons on the first screen, no scrolling
        self.assertFalse(d["stick"])                                                   # the toolbar row is still on screen: no extra bar

    def test_the_phone_scrolls_back_to_the_top_when_asked(self):
        R = self.go("phone", DEEP, "?printer=Printer-A", (390, 844))
        self.assertGreater(R["scrolled"], 0)
        self.assertEqual(R["afterShow"], 0)

    def test_10_inch_and_5_inch_show_the_block_with_its_buttons_without_any_scrolling(self):
        for name, query, size in (("kiosk", "?kiosk=1&printer=Printer-A", (1024, 600)), ("5in", "?layout=5in&printer=Printer-A", (800, 480)),
                                  ("bridge", "?layout=5in&theme=bridge&printer=Printer-A", (800, 480))):
            d = self.go(name, DEEP, query, size)["dl"]
            self.assertEqual((d["cur"], d["on"], d["scrollY"]), ("Printer-A", True, 0), name)
            self.assertTrue(d["act"] and d["pause"] and d["cancel"], name)
            self.assertLessEqual(d["cr"]["b"], d["vh"], name)
            self.assertLessEqual(d["cr"]["r"], d["vw"], name)
            self.assertEqual(d["docH"], d["vh"], name)

    def test_a_printer_without_a_warning_opens_as_before(self):
        d = self.go("calm", DEEP_CALM, "?layout=5in&printer=Printer-A", (800, 480))["dl"]
        self.assertEqual((d["cur"], d["on"], d["act"], d["spw"]), ("Printer-A", True, False, False))


# the phone's sticky bar
STICKY = SCREEN_PRO + r"""
await sleep(300);
const scrollTo2 = async y => { window.scrollTo(0, y); window.dispatchEvent(new Event('scroll')); await sleep(250); };         // (headless Chrome paints no frames, so the scroll event is sent by hand: the page's own listener does the work)
const sm = () => ({stk: document.getElementById('printer').classList.contains('stk'), vis: vis(document.getElementById('p-stick')), r: vis(document.getElementById('p-stick')) ? r(document.getElementById('p-stick')) : null,
  words: [...document.querySelectorAll('#p-stick button')].filter(vis).map(b => b.textContent.replace(/\s+/g, ' ').trim()), name: document.getElementById('stk-name').textContent,
  small: [...document.querySelectorAll('#p-stick button')].filter(vis).map(b => Math.round(b.getBoundingClientRect().height)), scrollY: Math.round(scrollY), docH: document.documentElement.scrollHeight,
  tbarH: r(document.querySelector('#printer .tbar')).h, fixed: getComputedStyle(document.getElementById('p-stick')).position, cw: document.documentElement.clientWidth, cut: [...document.querySelectorAll('#p-stick button')].filter(vis).filter(cut).length});
await open('Printer-A'); R.top = sm();
await scrollTo2(700); R.scrolled = sm();
window.__calls.length = 0; document.getElementById('stk-pause').click(); await sleep(300); R.pause = {calls: posts(), modal: mo()};
window.__calls.length = 0; document.getElementById('stk-cancel').click(); await sleep(150); R.cancel = {calls: posts().length, dlg: dlg()};
clickBtn(/Keep printing/); await sleep(100); R.keep = {calls: posts().length, open: mo()};
document.getElementById('stk-cancel').click(); await sleep(100); clickBtn(/CANCEL PRINT/); await sleep(400); R.sent = posts();
await scrollTo2(0); R.back = sm();
FLEET[0] = Object.assign({}, FLEET[0], {state: 'PAUSE'}); await pview(); await scrollTo2(700); R.paused = sm();
FLEET[0] = Object.assign({}, FLEET[0], {state: 'IDLE'}); await pview(); await scrollTo2(700); R.idle = sm();
document.body.classList.add('viewonly'); FLEET[0] = Object.assign({}, FLEET[0], {state: 'RUNNING'}); await pview(); await scrollTo2(700); R.viewonly = sm(); document.body.classList.remove('viewonly');
"""


class StickyBarTests(unittest.TestCase):
    R = {}

    def go(self, query, size):
        k = (query, size)
        if k not in StickyBarTests.R:
            StickyBarTests.R[k] = run_q(self, STICKY, query, size, 240000)
        R = StickyBarTests.R[k]
        self.assertNotIn("error", R, R.get("error"))
        return R

    def phone(self):
        return self.go("", (390, 844))

    def test_while_printing_and_scrolled_a_pause_cancel_bar_sticks_to_the_top(self):
        R = self.phone()
        self.assertGreater(R["scrolled"]["docH"], 844)
        s = R["scrolled"]
        self.assertTrue(s["stk"] and s["vis"])
        self.assertEqual((s["fixed"], s["r"]["t"], s["r"]["l"], s["r"]["r"]), ("fixed", 0, 0, s["cw"]))      # full page width (minus a CI scrollbar)
        self.assertGreaterEqual(s["cw"], 370)
        self.assertEqual(s["words"], ["Pause", "Cancel print"])
        self.assertEqual(s["name"], "Printer-A")
        self.assertGreaterEqual(min(s["small"]), 44)
        self.assertEqual(s["cut"], 0)
        self.assertLessEqual(s["r"]["h"], 72)

    def test_it_adds_no_row_while_the_toolbar_row_is_on_screen_and_none_when_not_printing(self):
        R = self.phone()
        self.assertFalse(R["top"]["stk"] or R["top"]["vis"])                           # at the top the toolbar's own Pause / Cancel are the row
        self.assertFalse(R["back"]["stk"] or R["back"]["vis"])
        self.assertEqual(R["back"]["tbarH"], R["top"]["tbarH"])
        self.assertFalse(R["idle"]["stk"] or R["idle"]["vis"])                         # an idle printer: nothing, however far you scroll
        self.assertEqual(R["idle"]["r"], None)

    def test_pause_is_one_tap_and_cancel_goes_through_the_one_confirm(self):
        R = self.phone()
        self.assertEqual(R["pause"], {"calls": ['POST /api/printer/Printer-A/action {"action":"pause"}'], "modal": False})
        self.assertEqual(R["cancel"]["calls"], 0)
        self.assertEqual(R["cancel"]["dlg"]["btns"], ["Keep printing", "CANCEL PRINT"])
        self.assertEqual(R["keep"], {"calls": 0, "open": False})
        self.assertEqual(R["sent"], ['POST /api/printer/Printer-A/action {"action":"cancel"}'])

    def test_a_paused_print_offers_resume_not_pause(self):
        s = self.phone()["paused"]
        self.assertTrue(s["stk"] and s["vis"])
        self.assertEqual(s["words"], ["Resume", "Cancel print"])

    def test_view_only_never_sees_it_and_the_wider_layouts_never_show_it(self):
        self.assertFalse(self.phone()["viewonly"]["vis"])
        k = self.go("?kiosk=1", (1024, 600))
        for key in ("top", "scrolled", "paused", "idle"):
            self.assertFalse(k[key]["vis"], key)


# the fleet page
FLEET_DRV = SCREEN_PRO + r"""
const verdict = (level) => ({watching: true, excluded: false, verdict: {level, score: 0.7, warmup: false}, alerts: [1]});
DET = Object.assign({}, DET, {printers: {'Printer-A': verdict('warning'), 'Printer-B': verdict('failure')}});
await sleep(300); await setFleet(FLEET);
const cardOf = n => [...document.querySelectorAll('#fleet .pcard')].find(c => c.dataset.name === n);
const qa = (c, s) => c ? [...c.querySelectorAll(s)] : [];
const fsnap = () => {
  const ban = document.getElementById('sp-alert'), cards = [...document.querySelectorAll('#fleet .pcard')];
  return {banner: {hidden: ban.hidden, text: ban.textContent, act: ban.dataset.act || null, name: ban.dataset.name || null, role: ban.getAttribute('role'), names: [...ban.querySelectorAll('.spn')].map(b => b.dataset.name + ':' + b.textContent),
      h: r(ban).h, cut: cut(ban), br: r(ban)},
    cards: cards.map(c => { const q = qa(c, '.spq'); return {name: c.dataset.name, spq: q.length, btns: qa(c, '.spq button').map(b => b.textContent.replace(/\s+/g, ' ').trim()), acts: qa(c, '.spq button').map(b => b.dataset.act),
      word: qa(c, '.spg.alert, .spag.alert').map(e => [e.dataset.act, e.dataset.name, e.getAttribute('role')]), tmp: qa(c, '.meta.tmp').length, rows: q.length ? new Set(qa(c, '.spq button').map(b => r(b).t)).size : 0,
      inside: q.length ? qa(c, '.spq button').every(b => r(b).b <= r(c).b && r(b).t >= r(c).t && r(b).r <= r(c).r && r(b).l >= r(c).l) : null, cut: qa(c, '.spq button').filter(cut).length,
      minH: q.length ? Math.min(...qa(c, '.spq button').map(b => r(b).h)) : null, fileLine: qa(c, '.file').length, h: r(c).h, b: r(c).b, spq5: q.length ? qa(c, '.spq button').map(b => r(b).w) : []}; }),
    vh: innerHeight, vw: document.documentElement.clientWidth, calm: cards.filter(c => !c.querySelector('.spq')).map(c => c.dataset.name)};
};
R.both = fsnap();
// a tap on a card's Pause: that printer's pause request, one tap, and the printer screen is NOT opened
window.__calls.length = 0; qa(cardOf('Printer-A'), '.spq-p')[0].click(); await sleep(300);
R.cardPause = {calls: posts(), home: document.getElementById('home').classList.contains('on'), cur: CUR, modal: mo()};
// a tap on the paused (failure) card's Cancel: the ONE confirm, about THAT printer
window.__calls.length = 0; qa(cardOf('Printer-B'), '.spq-c')[0].click(); await sleep(150);
R.cardCancel = {dlg: dlg(), calls: posts().length, home: document.getElementById('home').classList.contains('on')};
clickBtn(/Keep printing/); await sleep(100); R.cardKeep = {calls: posts().length, open: mo()};
qa(cardOf('Printer-B'), '.spq-c')[0].click(); await sleep(100); clickBtn(/CANCEL PRINT/); await sleep(400);
R.cardSent = {calls: posts(), home: document.getElementById('home').classList.contains('on'), open: mo()};
// the card's word opens the printer
qa(cardOf('Printer-A'), '.spg.alert, .spag.alert')[0].click(); await sleep(500);
R.wordOpen = {cur: CUR, on: document.getElementById('printer').classList.contains('on')};
show('home'); await sleep(300);
// the banner: with two warned printers each name is a button
qa(document.getElementById('sp-alert'), '.spn').find(b => b.dataset.name === 'Printer-A').click(); await sleep(500);
R.nameOpen = {cur: CUR, on: document.getElementById('printer').classList.contains('on')};
show('home'); await sleep(300);
CUR = null;
// one warned printer: the banner itself opens it
DET.printers['Printer-B'] = {watching: true, excluded: false, verdict: {level: 'ok', score: 0.1, warmup: false}, alerts: [0]}; await fleet(); await sleep(150);
R.one = fsnap();
document.getElementById('sp-alert').click(); await sleep(500);
R.bannerOpen = {cur: CUR, on: document.getElementById('printer').classList.contains('on')};
show('home'); await sleep(300);
// view-only: no Pause / Cancel on the card
DET.printers['Printer-A'] = verdict('warning'); await fleet(); await sleep(150);
document.body.classList.add('viewonly'); await sleep(50);
R.viewonly = {vis: qa(cardOf('Printer-A'), '.spq button').filter(vis).length, bannerAct: document.getElementById('sp-alert').dataset.act}; document.body.classList.remove('viewonly');
// the warning clears: the Nozzle / Bed line comes back
DET.printers['Printer-A'] = {watching: true, excluded: false, verdict: {level: 'ok', score: 0.1, warmup: false}, alerts: [0]}; await fleet(); await sleep(150);
R.cleared = fsnap();
"""


class FleetTests(unittest.TestCase):
    R = {}

    def go(self, key, query, size, wide=False):
        k = (key, query, size, wide)
        if k not in FleetTests.R:
            drv = FLEET_DRV.replace("await sleep(300); await setFleet(FLEET);", (WIDE if wide else "") + "await sleep(300); await setFleet(FLEET);", 1)
            FleetTests.R[k] = run_q(self, drv, query, size, 240000)
        R = FleetTests.R[k]
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["alerts"], [])
        return R

    LAYOUTS = (("kiosk", "?kiosk=1", (1024, 600)), ("5in", "?group=Home&layout=5in", (800, 480)), ("bridge", "?group=Home&layout=5in&theme=bridge", (800, 480)),
               ("desktop", "?view=desktop", (1440, 900)), ("phone", "", (390, 844)))

    def card(self, S, name):
        return [c for c in S["cards"] if c["name"] == name][0]

    def test_warned_cards_have_a_compact_pause_cancel_row_and_calm_cards_do_not(self):
        for name, query, size in self.LAYOUTS:
            S = self.go(name, query, size)["both"]
            a, b = self.card(S, "Printer-A"), self.card(S, "Printer-B")
            self.assertEqual((a["spq"], a["btns"], a["acts"]), (1, ["Pause", "Cancel"], ["cardPause", "cardCancel"]), name)       # running: both
            self.assertEqual((b["spq"], b["btns"], b["acts"]), (1, ["Cancel"], ["cardCancel"]), name)                                # paused: Cancel only
            self.assertEqual(a["rows"], 1, name)                                                                                     # ONE row
            self.assertTrue(a["inside"] and b["inside"], name)
            self.assertEqual((a["cut"], b["cut"]), (0, 0), name)
            for n in ("Printer-C", "Printer-D"):
                self.assertEqual(self.card(S, n)["spq"], 0, name)
            self.assertEqual(S["calm"], ["Printer-C", "Printer-D"], name)

    def test_the_row_takes_the_place_of_a_line_so_no_card_grows_off_screen(self):
        for name, query, size in self.LAYOUTS[:3]:
            S = self.go(name, query, size)["both"]
            hs = {c["h"] for c in S["cards"]}
            self.assertEqual(len(hs), 1, (name, hs))                                                                                 # every card keeps one height
            if name == "kiosk":
                self.assertLessEqual(max(c["b"] for c in S["cards"]), S["vh"], name)             # (the 5-inch grid scrolls under a banner, as it always did)
            a = self.card(S, "Printer-A")
            if name == "kiosk":
                self.assertEqual(a["tmp"], 0, name)                                                                                  # the Nozzle / Bed line gave way ...
                self.assertEqual(self.card(S, "Printer-C")["tmp"], 1, name)                                                          # ... on this card only
            if name != "kiosk":
                self.assertEqual(a["fileLine"] if name != "desktop" else 0, 0, name)                                                 # 5-inch: the file + layer lines gave way

    def test_with_a_wide_fallback_font_no_word_is_cut_and_every_card_keeps_one_height(self):
        for name, query, size in self.LAYOUTS[:3]:
            S = self.go(name, query, size, wide=True)["both"]
            for c in S["cards"]:
                self.assertEqual(c["cut"], 0, (name, c["name"]))
            self.assertEqual(len({c["h"] for c in S["cards"]}), 1, name)
            self.assertFalse(S["banner"]["cut"], name)

    def test_the_phone_row_is_a_48px_target(self):
        S = self.go("phone", "", (390, 844))["both"]
        self.assertGreaterEqual(self.card(S, "Printer-A")["minH"], 44)

    def test_the_banner_is_tappable_and_words_are_not_cut(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            b = R["both"]["banner"]
            self.assertFalse(b["hidden"], name)
            self.assertIn("SPAGHETTI FAILURE: Printer-B", b["text"], name)
            self.assertIn("WARNING: Printer-A", b["text"], name)
            self.assertIn("tap to open", b["text"], name)
            self.assertEqual((b["act"], b["name"], b["role"]), ("openPrinter", "Printer-B", "button"), name)                         # the first (FAILURE leads)
            self.assertEqual(b["names"], ["Printer-B:Printer-B", "Printer-A:Printer-A"], name)                                       # each name its own button
            self.assertFalse(b["cut"], name)
            self.assertLessEqual(b["br"]["r"], R["both"]["vw"], name)
            o = R["one"]["banner"]
            self.assertEqual((o["act"], o["name"], o["names"]), ("openPrinter", "Printer-A", []), name)                              # one printer: the banner itself is the button
            self.assertEqual(R["bannerOpen"], {"cur": "Printer-A", "on": True}, name)
            self.assertEqual(R["nameOpen"], {"cur": "Printer-A", "on": True}, name)

    def test_the_warning_word_on_a_card_is_a_button_that_opens_the_printer(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            a = self.card(R["both"], "Printer-A")
            self.assertEqual(a["word"], [["openPrinter", "Printer-A", "button"]], name)
            self.assertEqual(R["wordOpen"], {"cur": "Printer-A", "on": True}, name)

    def test_a_card_pause_is_one_tap_and_does_not_open_the_printer(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            self.assertEqual(R["cardPause"], {"calls": ['POST /api/printer/Printer-A/action {"action":"pause"}'], "home": True, "cur": None, "modal": False}, name)

    def test_a_card_cancel_goes_through_the_one_confirm_for_that_printer(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            c = R["cardCancel"]
            self.assertEqual(c["dlg"]["title"], "Cancel the print on Printer-B?", name)
            self.assertEqual(c["dlg"]["btns"], ["Keep printing", "CANCEL PRINT"], name)
            self.assertIn("Lid.gcode", c["dlg"]["text"], name)
            self.assertEqual((c["calls"], c["home"]), (0, True), name)
            self.assertEqual(R["cardKeep"], {"calls": 0, "open": False}, name)
            self.assertEqual(R["cardSent"], {"calls": ['POST /api/printer/Printer-B/action {"action":"cancel"}'], "home": True, "open": False}, name)

    def test_view_only_sees_no_buttons_and_the_card_goes_back_when_the_warning_clears(self):
        for name, query, size in self.LAYOUTS:
            R = self.go(name, query, size)
            self.assertEqual(R["viewonly"]["vis"], 0, name)
            c = R["cleared"]
            self.assertTrue(c["banner"]["hidden"] or "Printer-A" not in c["banner"]["text"], name)
            a = self.card(c, "Printer-A")
            self.assertEqual((a["spq"], a["word"]), (0, []), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
