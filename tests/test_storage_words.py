import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import bambu


class StorageWords(unittest.TestCase):
    def prn(self, model):
        return bambu.BambuPrinter("x", "127.0.0.1", "S", "code", model)

    def test_h2_series_is_a_usb_drive_with_a_note(self):
        for m in ("Bambu Lab H2D", "H2D", "h2s"):
            p = self.prn(m)
            self.assertEqual(p.storage_name, "USB drive", m)
            self.assertIn("internal storage", p.storage_note)

    def test_other_bambus_keep_sd_card_and_no_note(self):
        for m in ("Bambu Lab P1S", "Bambu Lab A1", "X1C", "", None):
            p = self.prn(m)
            self.assertEqual(p.storage_name, "SD card", m)
            self.assertEqual(p.storage_note, "")


if __name__ == "__main__":
    unittest.main()
