"""UI round 2: Show/Hide on secrets, "More options", one Save button on Edit printer, camera address field, camera
warning line, camera 503 text, Cancel only while printing, 5in fleet header alignment.

    python3 tests/test_ui_round2.py

Static checks read app/dashboard.html + app/fleet_store.py as text; the browser part loads the real dashboard into headless
Chrome with a stubbed fetch (skipped when no browser is found). Never touches a printer.
"""
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="ui2-test-")
os.environ["BAMBU_MCP_CONFIG"] = os.path.join(_TMP, "none.yml")
os.environ["BAMBU_MCP_CACHE"] = os.path.join(_TMP, "data", "metacache.json")

with open(os.path.join(APP, "dashboard.html"), encoding="utf8") as _fh:
    HTML = _fh.read()
with open(os.path.join(APP, "fleet_store.py"), encoding="utf8") as _fh:
    STORE = _fh.read()


class Static(unittest.TestCase):
    def test_show_hide_on_every_secret_input(self):
        self.assertIn('class="pm-show"', HTML)
        self.assertIn("btn.textContent = show ? 'Hide' : 'Show'", HTML)
        self.assertIn("pmShow:     b => pmShow(b)", HTML)
        self.assertIn("pmRemask();", HTML)                       # masked again when saving

    def test_more_options_is_words_not_color(self):
        self.assertIn("More options \\u25B8", HTML)
        self.assertIn("Fewer options \\u25BE", HTML)
        self.assertIn("f.advanced", HTML)

    def test_no_http_plus_copy_in_dashboard(self):
        self.assertNotRegex(HTML, r"http://\s*plus")
        self.assertNotIn("http:// plus", STORE)

    def test_one_save_button_on_edit(self):
        self.assertNotIn("Save groups now", HTML)
        self.assertNotIn("Groups saved.", HTML)
        self.assertNotIn("function pgSave", HTML)
        for word in ("'No changes'", "'Save changes'", "Save changes (test first)"):
            self.assertIn(word, HTML)
        self.assertIn("const PM_NONCONN = ['name', 'model', 'camera_url']", HTML)

    def test_camera_address_is_a_plain_text_box(self):
        self.assertIn("&& f.name !== 'camera_url'", HTML)
        self.assertIn("existing.camera_url", HTML)

    def test_camera_line_and_503_text(self):
        self.assertIn("<b>Camera:</b>", HTML)
        self.assertIn("camera_check", HTML)
        self.assertIn("function camWhy", HTML)
        self.assertIn("r.status === 503", HTML)

    def test_cancel_only_when_a_print_exists_everywhere(self):
        self.assertIn("$('p-cancel').style.display = BUSY_STATES.includes(s.state) ? '' : 'none'", HTML)

    def test_5in_cards_have_the_same_slots_with_or_without_a_camera(self):
        """Round 3: no per-count special case for a card without a camera any more - the picture slot is always there and
        says NO CAMERA / CAMERA OFFLINE in words, so name / number / bar / temps sit at the same offsets on every card."""
        self.assertNotRegex(HTML, r'#fleet\[data-n="[23]"\] \.pcard\[data-cam="0"\]')
        self.assertIn("NO CAMERA", HTML)
        self.assertIn("CAMERA OFFLINE", HTML)
        m = re.search(r"html\.lay-5in #fleet \.pcard \.prow \{([^}]*)\}", HTML)
        self.assertIsNotNone(m)
        self.assertIn("height:30px", m.group(1))
        self.assertNotIn("align-content:center", m.group(1))

    def test_bambu_help_points_to_camera_address(self):
        self.assertNotIn("which the Hub does \"\n             \"not support yet", STORE)
        self.assertIn("Camera address", STORE)
        self.assertNotIn("(no camera view)", STORE)


def _schema():
    import fleet_store
    return json.dumps({"types": [f.describe() for f in fleet_store._make_families().values()]})


DRIVER = r"""
const SCHEMA = %s;
const prn = {name: 'Core One', type: 'prusalink', type_label: 'PrusaLink', address: 'http://10.0.0.5', model: '', driver_available: true, loaded: true,
  secrets: {password: 'set'}, values: {name: 'Core One', url: '10.0.0.5', username: 'maker', storage: '', model: ''}, groups: ['Shop'], camera_url: 'rtsp://***@10.0.0.9/live'};
let testBody = {ok: true, message: 'Connected - Core One', camera_check: {ok: false, message: 'Camera problem: no picture came back'}, warning: 'no picture came back'};
window.__stubs.push((u, m, init) => {
  if(u === '/api/fleet') return J([]);
  if(u === '/api/ui-version') return J({v: '1'});
  if(u === '/api/auth/me') return J({});
  if(u === '/api/settings') return J({sections: []});
  if(u === '/api/groups') return J([{name: 'Shop', printers: ['Core One']}]);
  if(u === '/api/printers') return J({printers: [prn]});
  if(u.startsWith('/api/printer-types')) return J(SCHEMA);
  if(u === '/api/printers/test') return J({result: testBody});
  if(/^\/api\/printers\/[^/]+$/.test(u) && m === 'PUT') return J({printer: prn, changed: []});
  return null;
});
const wait = async (f, n) => { for(let i = 0; i < (n || 60) && !f(); i++) await sleep(100); };
const save = () => ({label: $('pm-save').textContent, disabled: $('pm-save').disabled, primary: $('pm-save').classList.contains('primary'), skipHidden: $('pm-skip').hidden});
const input = (id, v) => { $(id).value = v; $(id).dispatchEvent(new Event('input')); };
await openPrinters(); await wait(() => document.querySelector('.pmrow'));
await pmEdit('Core One'); await wait(() => $('pm-save'));
R.clean = save();
R.fieldIds = [...document.querySelectorAll('#pm-form [id^=pm-f-]')].map(e => e.id.slice(5));
R.moreHiddenAtStart = $('pm-more-body') ? $('pm-more-body').hidden : null;      // camera_url + username are non-default -> opens by itself
R.cameraType = $('pm-f-camera_url').type; R.cameraValue = $('pm-f-camera_url').value; R.cameraPlaceholder = $('pm-f-camera_url').placeholder;
R.pwType = $('pm-f-password').type;
document.querySelector('[data-act=pmShow]').click(); R.pwShown = [$('pm-f-password').type, document.querySelector('[data-act=pmShow]').textContent];
document.querySelector('[data-act=pmShow]').click(); R.pwHidden = [$('pm-f-password').type, document.querySelector('[data-act=pmShow]').textContent];
// 1) groups only -> save is live, no test needed
document.querySelector('.pg-chip [data-act=pgDrop]').click(); R.afterGroup = save();
$('pm-save').click(); await wait(() => window.__calls.some(c => c.startsWith('PUT /api/printers/Core One') || c.startsWith('PUT /api/printers/Core%%20One')));
R.putGroups = JSON.parse(window.__calls.filter(c => c.startsWith('PUT')).pop().split(' ').slice(2).join(' ')).values.groups;
await wait(() => PM.view === 'list'); await sleep(200);
// 2) camera only -> still no test
await pmEdit('Core One'); await wait(() => $('pm-save'));
input('pm-f-camera_url', 'rtsp://10.0.0.10/live'); R.afterCamera = save();
// 3) address -> needs a test first
input('pm-f-url', '10.0.0.6'); R.afterAddress = save();
$('pm-test').click(); await wait(() => $('pm-result').textContent.includes('Camera'));
R.testText = $('pm-result').textContent; R.afterTest = save();
// 4) put the address back -> only the camera is different again; the tested state is not needed
input('pm-f-url', '10.0.0.5'); input('pm-f-camera_url', 'rtsp://***@10.0.0.9/live'); R.back = save();
// 5) 'More options' stays shut for a plain printer
prn.camera_url = ''; prn.values.username = 'maker'; prn.values.storage = 'usb'; prn.secrets = {password: 'set'};
await openPrinters(); await wait(() => document.querySelector('.pmrow')); await pmEdit('Core One'); await wait(() => $('pm-save')); R.moreClosedPlain = $('pm-more-body').hidden;
document.getElementById('pm-more-btn').click(); R.moreOpened = [$('pm-more-body').hidden, $('pm-more-btn').textContent];
""" 


class Browser(unittest.TestCase):
    R = None

    def result(self):
        import test_fix_dashboard as tfd
        if Browser.R is None:
            Browser.R = tfd.run_page(self, DRIVER % _schema(), 60000)
        return Browser.R

    def test_form_behaviour(self):
        R = self.result()
        self.assertNotIn("error", R, R.get("error"))
        self.assertEqual(R["clean"], {"label": "No changes", "disabled": True, "primary": False, "skipHidden": True})
        self.assertEqual(R["fieldIds"][:3], ["name", "url", "password"])               # advanced ones come after
        self.assertEqual(R["moreHiddenAtStart"], False)                                # camera address already set -> opened
        self.assertEqual((R["cameraType"], R["cameraValue"]), ("text", "rtsp://***@10.0.0.9/live"))
        self.assertEqual(R["cameraPlaceholder"], "rtsp://192.168.1.60/live")
        self.assertEqual(R["pwType"], "password")
        self.assertEqual(R["pwShown"], ["text", "Hide"])
        self.assertEqual(R["pwHidden"], ["password", "Show"])
        self.assertEqual(R["afterGroup"], {"label": "Save changes", "disabled": False, "primary": True, "skipHidden": True})
        self.assertEqual(R["putGroups"], [])
        self.assertEqual(R["afterCamera"], {"label": "Save changes", "disabled": False, "primary": True, "skipHidden": True})
        self.assertEqual(R["afterAddress"], {"label": "Save changes (test first)", "disabled": True, "primary": False, "skipHidden": False})
        self.assertIn("Camera: WARNING", R["testText"])
        self.assertEqual(R["afterTest"]["label"], "Save changes")
        self.assertFalse(R["afterTest"]["disabled"])
        self.assertEqual(R["back"]["label"], "No changes")
        self.assertTrue(R["moreClosedPlain"])
        self.assertEqual(R["moreOpened"], [False, "Fewer options ▾"])


if __name__ == "__main__":
    unittest.main()
