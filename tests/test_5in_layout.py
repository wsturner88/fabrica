"""Static checks for the 5-inch wall screen layout (/dashboard?layout=5in, 800x480) and the tap-to-connect chip.

    python3 tests/test_5in_layout.py

Reads app/dashboard.html and app/server.py as text; starts nothing and opens no sockets (and never touches a printer).
The pixel proof (scroll height 480 on every primary screen, kiosk / phone unchanged) lives in the screenshots; here we pin
the rules that make it true: everything 5in is scoped under html.lay-5in, the boards are fixed grids, rare actions sit
behind the sheet, state is in words and progress is number + bar.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "..", "app", "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()
with open(os.path.join(HERE, "..", "app", "server.py"), encoding="utf8") as _fh:
    SERVER = _fh.read()

START = "/* ==== 5-inch wall screen: /dashboard?layout=5in"


def block():
    i = HTML.index(START)
    j = HTML.index("</style>", i)
    return HTML[i:j]


def css_rules(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def rule(selector_part):
    """The declaration text of the first rule whose selector list contains selector_part."""
    exact = selector_part.endswith(" {")
    part = selector_part[:-2] if exact else selector_part
    for sel, body in css_rules(block()):
        if (exact and part in [x.strip() for x in sel.split(",")]) or (not exact and part in sel):
            return body
    raise AssertionError("no 5in rule for " + selector_part)


def js_function(name):
    m = re.search(r"(?:async )?function " + name + r"\(", HTML)
    assert m, name
    i = HTML.index("{", m.end())
    depth, j = 0, i
    while True:
        c = HTML[j]
        depth += c == "{"
        depth -= c == "}"
        j += 1
        if depth == 0:
            return HTML[m.start():j]


class TestScoping(unittest.TestCase):
    def test_every_5in_rule_is_scoped_under_the_opt_in_class(self):
        loose = []
        for sel, _ in css_rules(block()):
            for one in [s.strip() for s in sel.split(",")]:
                if one.startswith("html.lay-5in") or one in (".more5", "@font-face"):      # @font-face only declares a font; nothing uses it outside the 5in rules
                    continue
                loose.append(one)
        self.assertEqual(loose, [], "5in rules that would also hit the kiosk / desktop / phone layouts")

    def test_layout_is_opt_in_only(self):
        self.assertIn("get('layout')", HTML)
        self.assertIn("classList.add('lay-5in')", HTML)
        self.assertNotRegex(HTML, r"matchMedia\([^)]*800")

    def test_new_global_rules_are_inert_off_the_5in_layout(self):
        # the new elements exist everywhere but only show under html.lay-5in; the stats wrapper is invisible to layout
        self.assertRegex(HTML, r"\.l5only \{ display:none; \}")
        self.assertRegex(HTML, r"\.sheet5 \{ display:none; \}")
        self.assertRegex(HTML, r"#p-stat, #p-prim \{ display:contents; \}")
        for el in ('id="p-act" class="l5only"', 'id="p-filesbtn"', 'id="p-more5"'):
            self.assertIn(el, HTML)


class TestPrinterBoard(unittest.TestCase):
    def test_printer_screen_is_a_fixed_800x480_grid(self):
        body = rule("html.lay-5in #printer.on")
        self.assertIn("display:grid", body)
        self.assertIn("overflow:hidden", body)
        self.assertIn("height:100dvh", body)
        self.assertIn("grid-template-columns:528px", body)
        self.assertIn("grid-template-rows:48px 297px 48px 56px", body)             # header . camera . control row . action strip = 480 with the padding
        self.assertIn('"tbar tbar" "cam stat" "row stat" "act act"', body)

    def test_camera_is_528_wide_16_9(self):
        self.assertIn("width:528px", rule("#p-topgrid > #cam"))
        self.assertIn("height:295px", rule("html.lay-5in #cam .camimg"))          # + 2 border = 297: 528 x 297 is 16:9
        self.assertIn("display:none", rule("html.lay-5in #cam .camhead"))     # caption no longer covers the picture

    def test_one_56px_action_strip(self):
        self.assertIn("height:56px", rule("html.lay-5in #p-act {"))
        self.assertIn("min-height:56px", rule("html.lay-5in #p-act button {"))
        self.assertIn("$('p-act').append($('p-ctl-btns'), $('p-filesbtn'), $('p-more5'))", HTML)

    def test_strip_is_state_dependent(self):
        fn = js_function("ctlRender")
        for needle in ("Pause", "Resume", "Connect", "Cancel&hellip;", "pmoreClose();", "ctlUnloadAsk()", "rare.push(gated("):
            self.assertIn(needle, fn)
        self.assertRegex(fn, r"if\(busy\) tail\.push\(.*askCancel\(\)")        # Cancel only while a print exists
        self.assertNotIn("Print a file &#9656;", fn)                              # Files is a fixed button in the strip (p-filesbtn), not a generated one

    def test_cancel_not_shown_while_idle_on_any_layout(self):
        self.assertIn('id="p-cancel"', HTML)
        self.assertIn("$('p-cancel').style.display = BUSY_STATES.includes(s.state) ? '' : 'none'", HTML)

    def test_old_tbar_buttons_are_hidden_on_5in(self):
        self.assertIn("display:none !important", rule("#printer .tbar > button:not(:first-child):not(.l5only)"))

    def test_detail_shows_percent_eta_layer_and_bar(self):
        fn = js_function("l5Detail")
        self.assertIn("Math.round(Number(s.percent) || 0) + '%'", fn)
        self.assertIn("finishAt(rem)", fn)
        self.assertIn("'Layer '", fn)
        self.assertIn("clampPct(s.percent)", fn)
        self.assertIn("font-size:56px", rule("html.lay-5in #p-big {"))

    def test_state_is_always_words(self):
        fn = js_function("l5Detail")
        self.assertIn("Math.round(Number(s.percent) || 0) + '%'", fn)
        for word in ("'Ready'", "'Not connected'", "'Offline'"):                  # not a colour: the word
            self.assertIn(word, fn)
        self.assertIn("function l5StateWord", HTML)
        self.assertIn("ALERT: ", HTML)                                                                       # spaghetti warning is worded too

    def test_temps_dim_the_target_and_say_off(self):
        self.assertIn("'/ off'", js_function("l5Detail"))
        self.assertIn("Number(tgt) > 0 ? ' /' + tnum(tgt) : ''", js_function("l5Tp"))      # a card shows the target only while there is one

    def test_slots_become_pills_with_a_camera(self):
        self.assertIn("$('p-row').insertBefore($('p-trays'), $('p-cells'))", HTML)     # the AMS slots sit in the control row under the camera
        self.assertIn('class="tray5"', HTML)


class TestFilesOverlayAndSheet(unittest.TestCase):
    def test_file_list_is_a_full_screen_overlay(self):
        self.assertIn("display:none", rule("html.lay-5in #printer #files {"))
        body = rule("#printer.files-open #files")
        for needle in ("position:fixed", "inset:0", "overflow-y:auto"):
            self.assertIn(needle, body)
        self.assertIn("function filesOpen()", HTML)
        self.assertIn("function filesClose()", HTML)
        self.assertIn("filesClose()", js_function("filesHead") if "function filesHead" in HTML else HTML)

    def test_overlay_sits_below_the_dialog_layer(self):
        z = int(re.search(r"z-index:(\d+)", rule("#printer.files-open #files")).group(1))
        modal = int(re.search(r"#modal \{[^}]*z-index:(\d+)", HTML).group(1))
        self.assertLess(z, modal)                      # Print / Delete confirmations open over the list

    def test_one_print_button_per_row_rename_delete_behind_the_name(self):
        self.assertIn("display:none", rule("html.lay-5in .frow .fdelete"))
        self.assertIn('data-act="fileMenu"', HTML)
        for act in ("fmPrint", "fmRename", "fmDelete"):
            self.assertIn(act, HTML)
        self.assertIn("height:53px", rule("html.lay-5in .frow {"))

    def test_rare_actions_live_in_the_more_sheet(self):
        for needle in ('data-act="pmBackup"', 'data-act="pmRefresh"', "Back up files", "Refresh details (slow)"):
            self.assertIn(needle, HTML)
        self.assertIn('id="pmore"', HTML)
        self.assertIn("min-height:56px", rule(".sheet5 .morebox button"))
        # none of the rare actions is left in the primary tbar on 5in
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", rule(".sheet5 .morebox"))


class TestFleet(unittest.TestCase):
    def test_grid_keys_for_1_to_4_printers(self):
        self.assertIn("n <= 4 ? String(Math.max(1, n))", js_function("l5Grid"))
        for n in ("1", "2", "6"):                                                   # 3-4 printers share the default 2x2 grid
            self.assertIn('#fleet[data-n="%s"]' % n, block())

    def test_cameras_on_the_cards_for_1_to_4_only(self):
        self.assertRegex(HTML, r"const l5Cams = n => n >= 1 && n <= 6;")             # pictures for up to 6 printers; 7+ = a placeholder, nothing fetched
        self.assertIn("!l5Cams(data.length)", HTML)
        self.assertIn("432px", rule('html.lay-5in #fleet[data-n="1"] .pcard .cslot {'))                  # big picture beside the big %
        self.assertIn("168px", rule('html.lay-5in #fleet .pcard .cslot {'))                              # the same 168x94 slot on every card
        for word in ("NO CAMERA", "CAMERA OFFLINE"):
            self.assertIn(word, js_function("fcAttach"))

    def test_big_percent_for_one_printer(self):
        self.assertIn("font-size:88px", rule('html.lay-5in #fleet[data-n="1"] .pcard .stat .big {'))

    def test_header_and_group_chips_are_one_row(self):
        self.assertIn("display:contents", rule("html.lay-5in #home > .row:first-child {"))
        self.assertIn("grid-row:1", rule("html.lay-5in #home > #fl-sw"))
        self.assertIn("grid-column:2", rule("html.lay-5in #home > #fl-sw"))

    def test_no_scroll_up_to_six_printers(self):
        self.assertIn("overflow:hidden", rule("html.lay-5in #home.on"))
        self.assertIn("minmax(0,1fr)", rule("html.lay-5in #fleet {"))
        self.assertIn("grid-auto-rows:204px", block())                 # 5+ printers (and a banner) scroll; cards never shrink

    def test_card_shows_word_percent_bar_and_layer(self):
        fn = js_function("l5Card") + js_function("cardParts")                       # the words of every card state live in cardParts (shared by every layout)
        self.assertIn("big = pct + '%'", fn)
        self.assertIn("big = 'IDLE'", fn)
        self.assertIn("'Layer ' + ly.replace('/', ' / ')", fn)
        self.assertIn('<div class="bar"><i style="width:', fn)

    def test_a_tap_on_a_5in_card_picture_opens_the_printer_not_a_second_screen(self):
        self.assertIn("if(L5) return; e.stopPropagation(); camOpenFull(name);", HTML)


class TestTapToConnect(unittest.TestCase):
    def test_fleet_api_says_who_can_connect(self):
        self.assertRegex(SERVER, r'"can_connect": hasattr\(prn, "connect_serial"\)\}?\s*#?[^\n]*\n\s*for prn in list\(PRINTERS\.values\(\)\)')

    def test_chip_words_say_tap_to_connect(self):
        self.assertIn("NOT CONNECTED</span><small>tap to connect</small>", HTML)
        self.assertIn('data-act="connectPrinter"', HTML)
        self.assertIn("connectPrinter: b => { if(!HUB.down) doConnect(b.dataset.name); }", HTML)

    def test_chip_only_for_a_printer_that_can_connect_and_never_view_only(self):
        fn = js_function("connectable")
        for needle in ("p.state === 'DISCONNECTED'", "canConn", "viewonly", "p.connected"):
            self.assertIn(needle, fn)
        self.assertIn("p.can_connect !== undefined ? !!p.can_connect : p.kind === 'octoprint'", HTML)

    def test_both_the_fleet_card_and_the_printer_page_use_it(self):
        self.assertIn("connectable(p, fleetCanConnect(p))", js_function("renderFleet"))
        self.assertIn("connectable(s, d.can_connect)", js_function("pviewOnce"))

    def test_doConnect_takes_a_printer_name_and_defaults_to_the_one_on_screen(self):
        fn = js_function("doConnect")
        self.assertIn("const PN = (typeof name === 'string' && name) ? name : CUR;", fn)
        self.assertNotIn("papi(CUR", fn)
        self.assertEqual(fn.count("papi(PN"), 2)
        # the existing buttons still call it with no argument
        self.assertGreaterEqual(HTML.count('onclick="doConnect()"'), 3)

    def test_never_auto_connects(self):
        # the only callers: a button / chip tap. Nothing in a poll or render path calls doConnect.
        for name in ("renderFleet", "fleet", "pviewOnce", "pview", "ctlRender", "l5Detail", "hubWatch"):
            self.assertNotIn("doConnect(", js_function(name).replace('onclick="doConnect()"', "").replace("onclick=\"doConnect()\"", ""), name)

    def test_stale_hub_keeps_the_chip_markup(self):
        self.assertIn("ch.dataset.html = ch.innerHTML", HTML)
        self.assertIn("ch.innerHTML = ch.dataset.html", HTML)

    def test_chip_is_keyboard_reachable(self):
        self.assertIn('role="button" tabindex="0" data-act="connectPrinter"', HTML)
        self.assertIn(".chip.conn[data-act]", HTML)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestJsHelpers(unittest.TestCase):
    def run_js(self, body):
        src = "\n".join([js_function("l5Grid").replace("$('fleet').dataset.n =", "global.out =").replace("l5Grid(n){", "l5Grid(n){"),
                         "console.log(JSON.stringify((" + body + ")))"])
        return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20, check=True).stdout)

    def test_l5Grid_maps_counts(self):
        out = []
        for n in (0, 1, 2, 3, 4, 5, 6, 7, 20):
            src = js_function("l5Grid").replace("$('fleet').dataset.n =", "global.v =") + "\nl5Grid(%d);console.log(global.v)" % n
            out.append(subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20, check=True).stdout.strip())
        self.assertEqual(out, ["1", "1", "2", "3", "4", "6", "6", "many", "many"])

    def test_connectable_rules(self):
        src = ("global.document={body:{classList:{contains:()=>global.VO}}};" + js_function("connectable") +
               ";const o=[];global.VO=false;"
               "o.push(connectable({connected:true,state:'DISCONNECTED'},true));"
               "o.push(connectable({connected:true,state:'DISCONNECTED'},false));"
               "o.push(connectable({connected:true,state:'IDLE'},true));"
               "o.push(connectable({connected:false,state:'UNKNOWN'},true));"
               "global.VO=true;o.push(connectable({connected:true,state:'DISCONNECTED'},true));console.log(JSON.stringify(o))")
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20, check=True)
        self.assertEqual(json.loads(r.stdout), [True, False, False, False, False])


if __name__ == "__main__":
    unittest.main()
