
from tempfile import NamedTemporaryFile
from unittest import TestCase

from sqlalchemy.sql.functions import count

from ch2.commands.activities import activities
from ch2.commands.args import bootstrap_file, m, V, DEV, mm, FAST
from ch2.commands.constants import constants
from ch2.config.default import default
from ch2.squeal.tables.activity import ActivityJournal
from ch2.squeal.tables.pipeline import PipelineType
from ch2.squeal.tables.statistic import StatisticJournal, StatisticJournalFloat, StatisticName
from ch2.stoats.pipeline import run_pipeline
from ch2.stoats.names import RAW_ELEVATION, ELEVATION


class TestActivities(TestCase):

    def test_activities(self):

        with NamedTemporaryFile() as f:

            args, log, db = bootstrap_file(f, m(V), '5')

            bootstrap_file(f, m(V), '5', mm(DEV), configurator=default)

            args, log, db = bootstrap_file(f, m(V), '5', 'constants', '--set', 'FTHR.%', '154')
            constants(args, log, db)

            args, log, db = bootstrap_file(f, m(V), '5', 'constants', 'FTHR.%')
            constants(args, log, db)

            args, log, db = bootstrap_file(f, m(V), '5', 'constants', '--set', 'SRTM1.dir',
                                           '/home/andrew/archive/srtm1')
            constants(args, log, db)

            args, log, db = bootstrap_file(f, m(V), '5', mm(DEV),
                                           'activities', mm(FAST), 'data/test/source/personal/2018-08-27-rec.fit')
            activities(args, log, db)

            # run('sqlite3 %s ".dump"' % f.name, shell=True)

            run_pipeline(log, db, PipelineType.STATISTIC, force=True, start='2018-01-01')

            # run('sqlite3 %s ".dump"' % f.name, shell=True)

            with db.session_context() as s:
                n_raw = s.query(count(StatisticJournalFloat.id)). \
                    join(StatisticName). \
                    filter(StatisticName.name == RAW_ELEVATION).scalar()
                self.assertEqual(2099, n_raw)
                n_fix = s.query(count(StatisticJournalFloat.id)). \
                    join(StatisticName). \
                    filter(StatisticName.name == ELEVATION).scalar()
                self.assertEqual(2079, n_fix)
                n = s.query(count(StatisticJournal.id)).scalar()
                self.assertEqual(29876, n)
                journal = s.query(ActivityJournal).one()
                self.assertNotEqual(journal.start, journal.finish)

    def test_segment_bug(self):
        with NamedTemporaryFile() as f:
            rgs, log, db = bootstrap_file(f, m(V), '5', mm(DEV), configurator=default)
            paths = ['/home/andrew/archive/fit/bike/2016-07-27-pm-z4.fit']
            run_pipeline(log, db, PipelineType.ACTIVITY, paths=paths, force=True)
