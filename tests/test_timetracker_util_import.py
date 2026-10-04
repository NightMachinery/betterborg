import os
import tempfile
import unittest


class TimetrackerUtilImportTests(unittest.TestCase):
    """The time tracker's helpers load as they do on a server.

    Under the tests no zsh startup file defines `isLocal`, so the module takes
    its server branch, where it configures plotly's image export.
    """

    def test_it_imports_with_any_plotly(self):
        #: Plotly 6 and later have no `plotly.io.orca`.
        from uniborg import timetracker_util

        self.assertFalse(timetracker_util.is_local)

    def test_its_database_stays_off_the_real_one(self):
        from uniborg import timetracker_util

        self.assertEqual(
            os.path.dirname(timetracker_util.timetracker_db_path),
            os.environ["timetracker_dir"],
        )
        self.assertTrue(
            str(timetracker_util.timetracker_db_path).startswith(tempfile.gettempdir())
        )


if __name__ == "__main__":
    unittest.main()
