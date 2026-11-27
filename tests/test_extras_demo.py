import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from astrapod_desktop import app as desktop, fakes
from astrapod_desktop.qt import QtCore, QtWidgets, Qt


def test_extras_on_demo_entry_point(monkeypatch, tmp_path):
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(directory))
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test"])
    create = desktop.create_application
    windows = []

    def capture(argv, services):
        services.appearance = fakes.FakeAppearance()
        services.daemon.tick = False
        result = create(argv, services)
        result[2].runner.inline = True
        windows.append(result[2])
        return result

    def smoke(_app):
        [window] = windows
        window.set_motion(False)
        window.navigate("extras")
        page = window.pages["extras"]
        assert page.isVisible()
        page.discover_button.click()
        assert page.calendars.count() == 1
        page.calendars.item(0).setCheckState(Qt.CheckState.Checked)
        page.calendar_sync.click()
        assert "同步 1 个日历" in page.status.text()

        vcf = tmp_path / "demo.vcf"
        vcf.write_bytes(b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Demo Contact\r\nN:Contact;Demo;;;\r\n"
                        b"UID:demo-card\r\nEND:VCARD\r\n")
        monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileNames",
                            lambda *_args, **_kwargs: ([str(vcf)], ""))
        page.tabs.setCurrentIndex(1)
        page.import_button.click()
        assert page.contacts.count() == 1
        page.contacts.setCurrentRow(0)

        def save_edit():
            from astrapod_desktop.pages.extras import ContactDialog
            dialog = page.findChild(ContactDialog)
            dialog.fields["NOTE"].setPlainText("Edited in demo")
            dialog.accept()

        QtCore.QTimer.singleShot(0, save_edit)
        page.edit_button.click()
        page.contact_sync.click()
        card = page.store.root.parent / "player/Contacts/AstraPod/demo-card.vcf"
        assert b"NOTE:Edited in demo" in card.read_bytes()
        assert not (tmp_path / "XDG_DATA_HOME/astrapod/extras").exists()
        window.close()
        application.processEvents()
        return 0

    monkeypatch.setattr(desktop, "create_application", capture)
    monkeypatch.setattr(QtWidgets.QApplication, "exec", smoke)
    assert desktop.main(["podlink_gui.py", "--demo"]) == 0
