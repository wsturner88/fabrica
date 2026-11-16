import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import bambu


class FtpsSessionReuse(unittest.TestCase):
    def _ftp(self, prot_p):
        f = bambu.ImplicitFTPTLS()
        f.host, f._prot_p = "printer", prot_p
        f.sock = mock.Mock()
        f.sock.session = object()
        return f

    def test_data_connection_reuses_the_control_session(self):
        f, raw = self._ftp(True), mock.Mock()
        with mock.patch.object(bambu.FTP, "ntransfercmd", return_value=(raw, 7)):
            conn, size = f.ntransfercmd("LIST")
        f.sock.context.wrap_socket.assert_called_once_with(raw, server_hostname="printer", session=f.sock.session)
        self.assertIs(conn, f.sock.context.wrap_socket.return_value)
        self.assertEqual(size, 7)

    def test_clear_data_connection_is_left_alone(self):
        f, raw = self._ftp(False), mock.Mock()
        with mock.patch.object(bambu.FTP, "ntransfercmd", return_value=(raw, None)):
            conn, _ = f.ntransfercmd("LIST")
        self.assertIs(conn, raw)
        f.sock.context.wrap_socket.assert_not_called()


if __name__ == "__main__":
    unittest.main()
