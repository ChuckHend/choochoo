import datetime as dt
from collections import namedtuple
from json import loads
from logging import getLogger

import numpy as np
from math import log10
from sqlalchemy import distinct
from sqlalchemy.sql.functions import count

from .utils import UniProcCalculator
from ..pipeline import LoaderMixin, OwnerInMixin
from ..read.segment import SegmentReader
from ...data.frame import statistics
from ...data.response import sum_to_hour, calc_response
from ...lib.date import round_hour, to_time, local_date_to_time, now
from ...names import Names as N
from ...sql import ActivityGroup
from ...sql import StatisticJournal, Composite, StatisticName, Source, Constant, CompositeComponent, \
    StatisticJournalFloat
from ...sql.utils import add

log = getLogger(__name__)

Response = namedtuple('Response', 'src_owner, title, tau_days, start, scale')

SCALED = 'scaled'


class ResponseCalculator(OwnerInMixin, LoaderMixin, UniProcCalculator):
    '''
    this is hard to do correctly, incrementally.

    for now, we do all or nothing.

    first, we check if the current solution is complete:
    * all sources used (no need to check for gaps - composite chaining should do that)
    * within 3 hours of the current time (if not, we regenerate, padding with zeroes)

    if not complete, we regenerate the whole damn thing.
    '''

    def __init__(self, *args, response_constants=None, prefix=None, **kargs):
        self.response_constant_names = self._assert('response_constants', response_constants)
        self.prefix = self._assert('prefix', prefix)
        super().__init__(*args, **kargs)

    def _startup(self, s):
        super()._startup(s)
        self.response_constants = [Constant.get(s, name) for name in self.response_constant_names]
        self.responses = [Response(**loads(constant.at(s).value)) for constant in self.response_constants]

    def _delete(self, s):
        start, finish = self._start_finish(local_date_to_time)
        if start or self.force:
            if finish:
                log.warning(f'Ignoring finish - will delete all impulses from {start} onwards')
            self._delete_from(s, start)

    def _delete_from(self, s, start=None):
        composite_ids = s.query(Composite.id). \
            join(StatisticJournal, Composite.id == StatisticJournal.source_id). \
            join(StatisticName, StatisticJournal.statistic_name_id == StatisticName.id). \
            filter(StatisticName.owner == self.owner_out)
        if start:
            composite_ids = composite_ids.filter(StatisticJournal.time >= start)
        log.debug(f'Delete query: {composite_ids}')
        n = s.query(count(Source.id)). \
            filter(Source.id.in_(composite_ids)). \
            scalar()
        if n:
            log.warning(f'Deleting {n} Composite sources ({start} onwards)')
            s.query(Source). \
                filter(Source.id.in_(composite_ids)). \
                delete(synchronize_session=False)
            s.commit()
            Composite.clean(s)

    def _missing(self, s):
        # clean out any gaps by unzipping the chained composites
        Composite.clean(s)
        # range we expect data for
        finish = round_hour(dt.datetime.now(tz=dt.timezone.utc), up=True)
        missing_sources = self.__missing_sources(s)
        missing_recent = any(self.__missing_recent(s, constant.short_name, finish)
                             for constant in self.response_constants)
        if missing_recent or missing_sources:
            if missing_recent: log.info('Incomplete coverage (so will re-calculate)')
            if missing_sources: log.info('Additional sources (so will re-calculate)')
            self._delete_from(s)
            start = round_hour(self.__start(s), up=False)
            return [(start, finish)]
        else:
            return []

    def __missing_recent(self, s, constant, now):
        finish = s.query(StatisticJournal.time). \
            join(StatisticName, StatisticJournal.statistic_name_id == StatisticName.id). \
            filter(StatisticName.name == self.prefix + '_' + constant,
                   StatisticName.owner == self.owner_out). \
            order_by(StatisticJournal.time.desc()).limit(1).one_or_none()
        if finish is None:
            log.debug(f'Missing data for {constant}')
            return True
        if (now - finish[0]).total_seconds() > 3 * 60 * 60:
            log.debug(f'Insufficient data for {constant}')
            return True
        return False

    def __missing_sources(self, s):
        response_ids = s.query(StatisticName.id). \
            filter(StatisticName.owner == self.owner_out)
        inputs = s.query(CompositeComponent.input_source_id). \
            join(StatisticJournal, StatisticJournal.source_id == CompositeComponent.output_source_id). \
            filter(StatisticJournal.statistic_name_id.in_(response_ids))
        unused = s.query(count(distinct(StatisticJournal.source_id))). \
            join(StatisticName, StatisticJournal.statistic_name_id == StatisticName.id). \
            filter(StatisticName.name == self.prefix + '_' + N.HR_IMPULSE_10,
                   StatisticName.owner == self.owner_in,
                   ~StatisticJournal.source_id.in_(inputs))
        missing = unused.scalar()
        if missing:
            if missing > 10:
                log.debug(f'Many ({missing}) missing')
            else:
                unused = s.query(Source). \
                    join(StatisticJournal). \
                    join(StatisticName, StatisticJournal.statistic_name_id == StatisticName.id). \
                    filter(StatisticName.name == self.prefix + '_' + N.HR_IMPULSE_10,
                           StatisticName.owner == self.owner_in,
                           ~StatisticJournal.source_id.in_(inputs))
                for source in unused.all():
                    log.debug(f'Missing {source}')
        return missing

    def __start(self, s):
        # avoid constants defined at time 0
        return s.query(StatisticJournal.time). \
            filter(StatisticJournal.time > 1.0). \
            order_by(StatisticJournal.time.asc()). \
            limit(1).scalar()

    def _run_one(self, s, missed):
        data = self.__read_data(s)
        if N.HR_IMPULSE_10 in data.columns and N.COVERAGE in data.columns:
            # coverage is calculated by the loader and seems to reflect records that have location data but not
            # HR data.  so i guess it makes sense to scale.
            # i don't remember why / when i added this - it might have been when using an optical monitor?
            # it seems like a relatively small effect in most cases.
            data.loc[now()] = {N.HR_IMPULSE_10: 0.0, N._src(N.HR_IMPULSE_10): None, N.COVERAGE: 100}
            data[SCALED] = data[N.HR_IMPULSE_10] * 100 / data[N.COVERAGE]
            all_sources = list(self.__make_sources(s, data))
            for constant, response in zip(self.response_constants, self.responses):
                name = self.prefix + '_' + constant.short_name
                log.info(f'Creating values for {response.title} ({name})')
                hr3600 = sum_to_hour(data, SCALED)
                params = (log10(response.tau_days * 24),
                          log10(response.start) if response.start > 0 else 1)
                result = calc_response(hr3600, params) * response.scale
                loader = self._get_loader(s, add_serial=False)
                source, sources = None, list(all_sources)
                for time, value in result.iteritems():
                    # the sources are much more spread out than the response, which is calculated every hour so
                    # that it is smooth.  so we only increment the source when necessary.
                    skipped = 0
                    while sources and time >= sources[0][0]:
                        source = sources.pop(0)[1]
                        skipped += 1
                        if skipped > 1:
                            log.warning(f'Skipping multiple sources at {time}')
                    loader.add(name, None, None, ActivityGroup.ALL, source, value, time,
                               StatisticJournalFloat, title=response.title,
                               description=f'The SHRIMP response for a decay of {response.tau_days} days')
                loader.load()

    def __read_coverage(self, s):
        names = s.query(StatisticName).filter(StatisticName.name == N._cov(N.HEART_RATE)).all()
        coverages = statistics(s, *names, owners=(SegmentReader,), check=False)
        coverages = coverages.loc[:].replace(0, np.nan)
        # extends the coverage across columns in both directions, so all columns are the same
        # (MTB contains entries from MTB, Road and Walk, for example)
        coverages.fillna(axis='columns', method='bfill', inplace=True)
        coverages.fillna(axis='columns', method='ffill', inplace=True)
        if not coverages.empty:
            # since all are the same, just take one
            coverages = coverages.iloc[:, [0]].rename(columns={coverages.columns[0]: N.COVERAGE})
        return coverages

    def __read_hr10(self, s):
        statistic_name = self.prefix + '_' + N.HR_IMPULSE_10
        df = statistics(s, statistic_name, with_sources=True, check=False, activity_group=ActivityGroup.ALL)
        df.rename(columns={statistic_name: N.HR_IMPULSE_10,
                           N._src(statistic_name): N._src(N.HR_IMPULSE_10)}, inplace=True)
        return df

    def __read_data(self, s):
        hr10 = self.__read_hr10(s)
        coverage = self.__read_coverage(s)
        # reindex and expand the coverage so we have a value at each impulse measurement
        coverage = coverage.reindex(index=hr10.index, method='nearest')
        data = hr10.join(coverage, how='left')
        if N.HR_IMPULSE_10 in data.columns and N.COVERAGE in data.columns:
            data.loc[:, [N.HR_IMPULSE_10]] = data.loc[:, [N.HR_IMPULSE_10]].fillna(0.0,)
            data.loc[:, [N.COVERAGE]] = data.loc[:, [N.COVERAGE]].fillna(axis='index', method='ffill')
            data.loc[:, [N.COVERAGE]] = data.loc[:, [N.COVERAGE]].fillna(axis='index', method='bfill')
        return data

    def __make_sources(self, s, data):
        # this chains forwards from zero, adding a new composite for each new impulse source.
        # in theory we could reconstruct only missing entries.
        log.info('Creating sources')
        name = N._src(N.HR_IMPULSE_10)
        prev = add(s, Composite(n_components=0))
        yield to_time(0.0), prev
        # find times where the source changes
        changes = data.loc[data[name].ne(data[name].shift())]
        for time, row in changes.iterrows():
            id = row[name]
            if not np.isnan(id):
                composite = add(s, Composite(n_components=2))
                add(s, CompositeComponent(input_source_id=id, output_source=composite))
            else:
                composite = add(s, Composite(n_components=1))
            add(s, CompositeComponent(input_source=prev, output_source=composite))
            yield time, composite
            prev = composite
        s.commit()
