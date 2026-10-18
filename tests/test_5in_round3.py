"""Round 3 of the 5-inch wall screen (/dashboard?layout=5in): the two looks (Quiet Grid default, Bridge theme), the per-printer
Spaghetti switch, the pre-heat presets, the control-row logic and the live camera with its snapshot fallback.

    python3 tests/test_5in_round3.py

Reads app/dashboard.html as text and runs the pure parts of its script under node (skipped without node). Starts nothing,
opens no sockets, never touches a printer. The pixel proof (screenshots next to the mockups, scrollHeight 480, the other layouts
pixel-identical) lives in preview-shots/round3/.
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


def css():
    i = HTML.index(START)
    return HTML[i:HTML.index("</style>", i)]


def css_rules(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text)]


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


def js_const(name):
    """`const NAME = ...;` up to the end of its statement (one line or a multi-line array / object)."""
    m = re.search(r"^const " + name + r"\b.*?;\s*$", HTML, flags=re.S | re.M)
    assert m, name
    return m.group(0)


def node(src):
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


HAVE_NODE = bool(shutil.which("node"))


class TestThemeSelection(unittest.TestCase):
    def test_default_is_quiet_and_bridge_is_a_class_on_html(self):
        self.assertIn("document.documentElement.classList.add('lay-5in')", HTML)
        self.assertIn("if(L5T === 'bridge') document.documentElement.classList.add('th-bridge')", HTML)
        self.assertIn("const L5T = L5 ? l5Theme(location.search) : '';", HTML)          # never evaluated off the 5in layout

    @unittest.skipUnless(HAVE_NODE, "node not installed")
    def test_url_wins_and_is_remembered(self):
        src = js_function("l5Theme") + """
        const mk = (init) => { const d = Object.assign({}, init || {}); return {d, getItem:k => (k in d ? d[k] : null), setItem:(k, v) => { d[k] = v; }}; };
        const out = [];
        let st = mk(); out.push(l5Theme('?layout=5in', st));                                  // nothing: Quiet
        st = mk(); out.push(l5Theme('?layout=5in&theme=bridge', st), st.d['fabrica.l5theme']);   // URL wins + remembered
        st = mk({'fabrica.l5theme': 'bridge'}); out.push(l5Theme('?layout=5in', st));          // remembered per screen
        st = mk({'fabrica.l5theme': 'bridge'}); out.push(l5Theme('?layout=5in&theme=quiet', st), st.d['fabrica.l5theme']);   // URL beats the memory, and replaces it
        st = mk({'fabrica.l5theme': 'bridge'}); out.push(l5Theme('?layout=5in&theme=BRIDGE', st));   // case-insensitive
        st = mk(); out.push(l5Theme('?layout=5in&theme=C', st), st.d['fabrica.l5theme'] || null);    // unknown name: ignored, not remembered
        st = mk({'fabrica.l5theme': 'weird'}); out.push(l5Theme('?layout=5in', st));
        const boom = {getItem(){ throw new Error('blocked'); }, setItem(){ throw new Error('blocked'); }};
        out.push(l5Theme('?layout=5in&theme=bridge', boom), l5Theme('?layout=5in', boom));         // storage that throws: the URL still works, default otherwise
        console.log(JSON.stringify(out));
        """
        self.assertEqual(node(src), ["quiet", "bridge", "bridge", "bridge", "quiet", "quiet", "bridge", "quiet", None, "quiet", "bridge", "quiet"])

    def test_storage_access_is_wrapped_in_try_catch(self):
        fn = js_function("l5Theme")
        self.assertEqual(fn.count("try{"), 3)
        self.assertIn("fabrica.l5theme", fn)

    def test_every_bridge_rule_is_scoped_under_both_classes(self):
        i = css().index("/* ==== Bridge theme")
        loose = []
        for sel, _ in css_rules(css()[i:]):
            for one in [s.strip() for s in sel.split(",")]:
                if not one.startswith("html.lay-5in.th-bridge"):
                    loose.append(one)
        self.assertEqual(loose, [], "Bridge rules that would also skin the default look")

    def test_bridge_reuses_the_default_dom(self):
        # no second set of screens: the frame pieces are decoration only, the rest is the same elements restyled
        self.assertIn('aria-hidden="true"', js_const("FR5"))
        self.assertRegex(css(), r"html\.lay-5in \.fr5 \{ display:none; \}")                  # hidden unless the theme draws them
        self.assertNotIn(".th-bridge", HTML[:HTML.index(START)])                                # no skin rules outside the 5in block

    def test_bridge_font_falls_back_to_a_condensed_system_font(self):
        body = [b for sel, b in css_rules(css()) if sel.strip() == "html.lay-5in.th-bridge"][0]
        m = re.search(r"--fs:([^;]*);", body)
        self.assertTrue(m.group(1).startswith("Antonio,"))
        for fallback in ("Arial Narrow", "Roboto Condensed", "sans-serif-condensed"):
            self.assertIn(fallback, m.group(1))
        self.assertRegex(css(), r"html\.lay-5in\.th-bridge body \{[^}]*font-stretch:condensed")

    def test_fonts_ship_with_the_hub_not_from_the_internet(self):
        for f in ("Inter-latin.woff2", "JetBrainsMono-latin.woff2", "Antonio-latin.woff2"):
            self.assertIn("url(/static/brand/" + f + ")", css())
            self.assertTrue(os.path.isfile(os.path.join(HERE, "..", "app", "static", "brand", f)), f)
        self.assertNotIn("fonts.googleapis.com", HTML)
        self.assertIn('".woff2": "font/woff2"', SERVER)                  # the brand route serves them (CSP: fonts from this origin only)

    def test_state_stays_a_word_in_both_looks(self):
        # the mockups' rule: the state is always a word (plus a shape), never colour alone - the chips carry their <i> shape and the words
        fn = js_function("l5Card")
        self.assertIn('<i></i>${esc(word)}', fn)
        self.assertIn("Spaghetti: <b>${esc(spag.word)}</b>", fn)
        self.assertIn("NOT CONNECTED", js_const("CONN_HTML5"))


@unittest.skipUnless(HAVE_NODE, "node not installed")
class TestSpaghetti(unittest.TestCase):
    def info(self, o):
        return node(js_function("spagInfo") + "\nconsole.log(JSON.stringify(spagInfo(%s)))" % json.dumps(o))

    def test_words(self):
        base = {"cam": True, "enabled": True, "excluded": False, "down": False, "learn": False, "running": True}
        self.assertEqual(self.info(dict(base))["word"], "WATCHING")
        self.assertEqual(self.info(dict(base, learn=True))["word"], "LEARNING")
        self.assertEqual(self.info(dict(base, running=False))["word"], "READY")
        self.assertEqual(self.info(dict(base, excluded=True))["word"], "OFF")
        self.assertEqual(self.info(dict(base, enabled=False))["word"], "OFF")
        self.assertEqual(self.info(dict(base, cam=False))["word"], "NO CAMERA")
        self.assertEqual(self.info(dict(base, down=True))["word"], "DETECTOR DOWN")
        self.assertEqual(self.info(dict(base, level="warning"))["word"], "WARNING")
        self.assertEqual(self.info(dict(base, level="failure"))["word"], "FAILURE")

    def test_precedence_no_camera_beats_everything_and_off_beats_watching(self):
        self.assertEqual(self.info({"cam": False, "enabled": False, "excluded": True})["k"], "nocamera")
        self.assertEqual(self.info({"cam": True, "enabled": True, "excluded": True, "running": True, "level": "failure"})["k"], "off")

    def test_toggle_adds_and_removes_from_the_excluded_list_case_insensitively(self):
        src = js_function("spagNextList") + """
        console.log(JSON.stringify([spagNextList(['Printer-B'], 'Printer-A', false), spagNextList(['Printer-B', 'printer-a'], 'Printer-A', true),
          spagNextList(['Printer-B'], 'Printer-A', true), spagNextList(null, 'Printer-A', false), spagNextList(['Printer-A', 'Printer-A'], 'printer-a', true)]))
        """
        self.assertEqual(node(src), [["Printer-B", "Printer-A"], ["Printer-B"], ["Printer-B"], ["Printer-A"], []])

    def test_switch_writes_through_the_existing_settings_route(self):
        fn = js_function("spagToggle")
        self.assertIn("api('/api/settings')", fn)
        self.assertIn("method:'PUT'", fn)
        self.assertIn("'detection.excluded_printers'", fn)
        self.assertIn("spagNextList(cur, me, !watching)", fn)
        self.assertNotIn("/api/detection", fn)                      # no new route, nothing else is written
        self.assertIn('data-ctl', re.search(r'<button id="sp-tog"[^>]*>', HTML).group(0))     # a view-only sign-in does not see a switch it cannot use
        self.assertIn('role="switch"', HTML)

    def test_switch_is_disabled_without_a_camera_or_while_detection_is_off(self):
        self.assertIn("$('sp-tog').disabled = !hasDet || info.k === 'nocamera' || (x && x.state === 'off')", js_function("l5Detail"))

    def test_fleet_cards_read_the_detector_status_read_only(self):
        self.assertIn("pollJson('/api/detection', 4000)", js_function("det5Fetch"))
        self.assertIn("det5Fetch()", js_function("fleet"))                                # every layout asks, not only the 5-inch one
        self.assertNotIn("L5 ? det5Fetch()", js_function("fleet"))
        src = js_function("det5For") + """
        const DET5 = {d: {enabled: true, mode: 'learn', detector: {state: 'up'}, setup: {excluded_printers: ['Printer-D']}, printers: {
          'Printer-B': {excluded: false}, 'Printer-C': {excluded: true},
          'Printer-E': {excluded: false, watching: true, verdict: {level: 'warning', score: 0.7, warmup: false}},
          'Printer-F': {excluded: false, watching: true, verdict: {level: 'failure', score: 0.9, warmup: false}},
          'Printer-G': {excluded: false, watching: true, verdict: {level: 'warning', score: 0.7, warmup: true}},      // still warming up: no word yet (the Hub's own rule)
          'Printer-H': {excluded: false, watching: false, verdict: {level: 'warning', score: 0.7, warmup: false}}}}};   // not watching now: an old verdict says nothing
        console.log(JSON.stringify([det5For('printer-b'), det5For('Printer-C').excluded, det5For('printer-d').excluded, det5For('zzz').excluded,
          ['Printer-E', 'Printer-F', 'Printer-G', 'Printer-H'].map(n => det5For(n).level)]))
        """
        r = node(src)
        self.assertEqual(r[0], {"enabled": True, "learn": True, "down": False, "level": "", "relearn": False, "excluded": False})
        self.assertEqual(r[1:4], [True, True, False])
        self.assertEqual(r[4], ["warning", "failure", "", ""])                              # the fleet card finally knows about a WARNING / FAILURE


@unittest.skipUnless(HAVE_NODE, "node not installed")
class TestPresetsAndControlRow(unittest.TestCase):
    def test_presets_are_the_hubs_bed_temperatures_not_a_copy(self):
        # Owner decision 10/17: pre-heat heats the BED only and there is one set of numbers - the Hub's Settings (printers.preheat_bed_*), delivered in
        # controls.detail.preheat.presets. The page keeps no temperatures of its own.
        self.assertNotIn("L5_PRESETS", HTML)
        src = js_function("l5Presets") + """
        var CTL = {d: {controls: {detail: {preheat: {presets: {PLA: {nozzle: null, bed: 55}, PETG: {nozzle: null, bed: 80}, ABS: {nozzle: null, bed: 90}}}}}}};
        const a = l5Presets(); CTL.d = null; const b = l5Presets(); CTL.d = {controls: {detail: {}}}; const c = l5Presets();
        console.log(JSON.stringify([a, b, c]));"""
        self.assertEqual(node(src), [[["PLA", 55], ["PETG", 80], ["ABS", 90]], [], []])
        self.assertNotRegex(js_function("l5Presets") + js_function("ctlPreheatPreset") + js_function("ctlPreheat"), r"\b(215|240|250|255)\b")

    def test_a_preset_is_sent_by_name_after_a_confirmation_and_never_carries_a_nozzle(self):
        self.assertIn("preheatGo: b => { if(b.dataset.p) ctlDo('preheat', {preset:b.dataset.p}); }", HTML)    # the Hub looks the bed temperature up
        self.assertNotIn("nozzle:pr[1]", HTML)
        self.assertIn('data-act="preheatGo"', js_function("ctlPreheatPreset"))
        self.assertIn("Pre-heat ${pmEsc(CUR)} for ${pmEsc(pr[0])}?", js_function("ctlPreheatPreset"))      # a tap on a cell asks first
        self.assertIn("The nozzle stays off.", js_function("ctlPreheatPreset"))
        self.assertIn('data-act="preheatPreset"', js_function("l5Detail"))
        self.assertIn('<span class="l">${esc(p[0])} bed</span>', js_function("l5Detail"))                  # words: "PLA bed 60°"
        self.assertNotRegex(js_function("ctlPreheatPreset"), r"onclick=\"[^\"]*\$\{")             # no values spliced into an inline handler
        self.assertNotIn("Nozzle ${", js_function("ctlPreheat"))                                          # the 10-inch two-tap sheet shows the bed only

    def test_preset_cells_follow_what_the_printer_can_do(self):
        fn = js_function("l5Detail")
        self.assertIn("acts.includes('preheat')", fn)                                               # no pre-heat in the driver = no cells (PrusaLink has none)
        self.assertIn("allowed.has('preheat') ? '' :", fn)                                         # busy / not ready = disabled with the reason
        self.assertIn(" disabled title=", fn)

    def test_row_modes(self):
        src = js_function("l5RowMode") + """
        console.log(JSON.stringify([l5RowMode(true, true, 0, false), l5RowMode(true, true, 4, true), l5RowMode(true, false, 0, true),
          l5RowMode(true, false, 0, false), l5RowMode(false, false, 0, true), l5RowMode(true, false, 4, true)]))
        """
        # printing: Left/Layer/Done . AMS printer: its slots (any state) . idle + can pre-heat: presets . idle, nothing to offer: none (the panel grows) . offline: none
        self.assertEqual(node(src), ["read", "trays", "preset", "none", "none", "trays"])

    def test_no_dead_buttons_for_fan_speed_flow_or_z(self):
        # no driver reports them and the Hub has no command for them: omitted, not shown as buttons that do nothing
        for word in ("Fan", "Speed", "Flow", "Z-offset"):
            self.assertNotIn(">%s<" % word, js_function("l5Detail"))
        self.assertNotIn("fan_speed", HTML)


@unittest.skipUnless(HAVE_NODE, "node not installed")
class TestLiveCamera(unittest.TestCase):
    """The detail screen shows the MJPEG stream; snapshots are the fallback. Run lvStart / lvOk / lvFail / lvStop against a fake <img> and a fake clock."""

    HARNESS = """
    const L5 = true, BLANK = 'blank:', CAM = {has: true, full: false}, CAM_ORI = {}, CUR = 'Printer-A';
    let now = 1000000, fetches = 0; Date.now = () => now;
    const timers = []; global.setInterval = (f, ms) => { timers.push({f, ms}); return timers.length; }; global.clearInterval = id => { if(timers[id - 1]) timers[id - 1].dead = true; };
    const run = ms => { for(let t = 0; t < ms; t += 500){ now += 500; timers.forEach(x => { if(!x.dead) x.f(); }); } };
    const cls = new Set(), img = {src: '', naturalWidth: 0, naturalHeight: 0, style: {}, onerror: null, classList: {add: c => cls.add(c), remove: c => cls.delete(c)}};
    const camBox = {classList: {add: c => cls.add('cam.' + c), remove: c => cls.delete('cam.' + c)}}, note = {style: {}}, stat = {textContent: ''};
    const $ = id => ({'cam-live': img, cam: camBox, 'cam-note': note, 'cam-stat': stat}[id]);
    const camFlip = () => '', camUrl = (k, n) => '/api/printer/' + n + '/' + k, camWanted = () => true, camFetch = () => { fetches++; }, l5Meta = () => {};
    """

    def src(self, body):
        i = HTML.index("const LV_FIRST_MS")
        block = HTML[i:HTML.index("// ---- fleet cards", i)]
        return self.HARNESS + block + "\n" + body

    def test_a_frame_in_time_means_live_and_no_snapshots(self):
        r = node(self.src("""
        lvStart(); const url = img.src; run(1000); img.naturalWidth = 640; img.naturalHeight = 480; run(1000);
        console.log(JSON.stringify([url.startsWith('/api/printer/Printer-A/stream?t='), LV.ok, cls.has('has'), cls.has('cam.live'), stat.textContent, fetches]))"""))
        self.assertEqual(r, [True, True, True, True, "640×480", 0])

    def test_no_frame_in_8_seconds_falls_back_to_snapshots_and_drops_the_stream(self):
        r = node(self.src("""
        lvStart(); run(7500); const before = [LV.on, fetches]; run(1000);
        console.log(JSON.stringify([before, LV.on, LV.ok, img.src, fetches, LV.failedAt > 0, cls.has('has')]))"""))
        self.assertEqual(r, [[True, 0], False, False, "blank:", 1, True, False])

    def test_a_stream_error_falls_back_the_same_way(self):
        r = node(self.src("""
        lvStart(); img.naturalWidth = 640; run(1000); const ok = LV.ok; img.onerror(); 
        console.log(JSON.stringify([ok, LV.ok, LV.on, img.src, fetches]))"""))
        self.assertEqual(r, [True, False, False, "blank:", 1])

    def test_a_failed_stream_is_retried_only_after_the_wait(self):
        r = node(self.src("""
        lvStart(); run(9000); const a = LV.on; now += 10000; lvStart(); const b = LV.on; lvStop(); now += 40000; lvStart(); const c = LV.on;
        console.log(JSON.stringify([a, b, c]))"""))
        self.assertEqual(r, [False, False, True])

    def test_leaving_the_screen_drops_the_connection(self):
        r = node(self.src("""
        lvStart(); img.naturalWidth = 640; run(1000); lvStop(); const s = img.src; lvStart(); lvStop(); lvStop();
        console.log(JSON.stringify([s, LV.on, LV.ok, cls.has('cam.live')]))"""))
        self.assertEqual(r, ["blank:", False, False, False])

    def test_a_late_event_from_an_old_stream_is_ignored(self):
        r = node(self.src("""
        lvStart(); const old = img.onerror; lvStop(); lvStart(); old(); const stillOn = LV.on; 
        console.log(JSON.stringify([stillOn, LV.failedAt]))"""))
        self.assertEqual(r, [True, 0])

    def test_wiring_into_the_camera_sync(self):
        sync = js_function("camSync")
        self.assertIn("if(camWanted() && !CAM.full) lvStart(); else lvStop();", sync)              # every layout, only while looked at (camWanted: this screen, page visible, display touched), not under the enlarged view
        self.assertNotIn("if(L5)", sync)
        self.assertIn("if(LV.ok) return;", js_function("camFetch"))                                # stills pause while the stream shows
        self.assertIn("lvStop()", js_function("camReset"))
        self.assertIn("LV_REFRESH_MS", js_function("camTick"))                                    # a stream that went quiet is reopened
        # no layout guard any more: the 10-inch kiosk, desktop and phone show the live picture too
        self.assertIn("if(LV.on || !CAM.has) return;", js_function("lvStart"))
        for fn in ("lvStart", "camTick", "camFetch", "camSync"):
            self.assertNotIn("L5", js_function(fn), fn)

    def test_the_in_page_picture_is_the_existing_stream_endpoint(self):
        self.assertIn("camUrl('stream', CUR)", js_function("lvStart"))
        self.assertIn('id="cam-live"', HTML)


@unittest.skipUnless(HAVE_NODE, "node not installed")
class TestFilesSheetLogic(unittest.TestCase):
    def test_sorting_and_available_keys(self):
        src = js_function("filesMinutes") + js_function("fileActivity") + js_function("filesSorted") + js_function("filesKeys") + """
        const f = [{name:'b', size:5, modified:10}, {name:'a', size:9, modified:30}, {name:'c', size:1, modified:20}];
        const h = [{name:'old', modified:10, last_printed:50}, {name:'new', modified:40}, {name:'mid', modified:30, last_printed:20}, {name:'none'}];
        const g = [{name:'x', print_time:'2h 05m'}, {name:'y', print_time:'45m'}, {name:'z'}, {name:'w', print_time:'1d 1h'}];
        console.log(JSON.stringify([filesSorted(f, 'name').map(x => x.name), filesSorted(f, 'size').map(x => x.name), filesSorted(f, 'recent').map(x => x.name),
          filesSorted(h, 'latest').map(x => x.name), filesKeys(f), filesKeys([{name:'x'}]), filesKeys([{name:'x', size:3}]), filesSorted(g, 'time').map(x => x.name), filesKeys(g),
          ['1h 20m', '45m', '2d 3h', '1:20:05', '', 'soon'].map(t => filesMinutes({print_time: t}))]))
        """
        self.assertEqual(node(src), [["a", "b", "c"], ["a", "b", "c"], ["a", "c", "b"], ["old", "new", "mid", "none"], ["latest", "recent", "name", "size"], ["name"], ["name", "size"],
                                     ["y", "x", "w", "z"], ["name", "time"], [80, 45, 3060, 80, None, None]])        # Time = print time, shortest first, unknown last

    def test_one_print_per_row_and_rename_delete_stay_behind_the_name(self):
        row = js_function("fileRowHtml")
        self.assertEqual(row.count('class="primary"'), 1)
        self.assertIn('data-act="fileMenu"', row)
        self.assertIn('class="nowtag">Printing', row)                      # the running job's row says so instead of offering Print again


class TestOtherLayoutsAreUntouched(unittest.TestCase):
    def test_new_markup_is_inert_off_the_5in_layout(self):
        # only the pieces that exist for the 5-inch board alone stay l5only; the progress card, tiles, Spaghetti switch, status panel and the
        # camera badges are shared by every layout now (their own CSS places them: see the parity block)
        for el in ('id="p-row" class="l5only"', 'id="p-kind" class="l5only"', 'id="p-clock" class="l5only"'):
            self.assertIn(el, HTML)
        self.assertRegex(HTML, r"#p-stat, #p-prim \{ display:contents; \}")        # the progress card wrapper is invisible to the kiosk / phone layouts
        self.assertRegex(HTML, r"\.l5only \{ display:none; \}")
        self.assertIn('<span class="bdg tl"><i></i><span id="cam-badge-t">', HTML)           # the LIVE / SNAPSHOT badge is on every layout

    def test_functions_that_changed_still_behave_the_same_without_the_layout(self):
        self.assertIn("if(L5) return l5Card(p, i);", js_function("renderFleet"))
        self.assertIn("on ? '&#10003; ' : ''", HTML)                               # the group chip has a tick on the active one on EVERY layout (colorblind rule)
        self.assertNotIn("on && !L5 ? '&#10003; '", HTML)
        self.assertIn("const filesHead = extra => L5\n", HTML)
        self.assertIn(": '<div class=\"fhead\">Jobs on SD card' + (extra ? '<small>' + extra + '</small>' : '') +", HTML)    # the old header, now with its "More" button (back up / refresh) in the same row


if __name__ == "__main__":
    unittest.main()


def test_5in_fleet_stills_never_idle_pause():
    """Wall screen nobody touches: fleet stills must keep refreshing (the pause note is hidden on 5in)."""
    import pathlib, re
    html = (pathlib.Path(__file__).resolve().parents[1] / "app" / "dashboard.html").read_text()
    body = html[html.index("function camIdleCheck(){"):]
    body = body[:body.index("\n}\n")]
    assert re.search(r"if\(L5 && !CAM\.full && \$\('home'\)\.classList\.contains\('on'\)\) return false;", body)
    assert body.index("return false;   // 5-inch") < body.index("CAM.paused = true")
