
from collections import defaultdict
from logging import getLogger

from sqlalchemy import Column, Integer, ForeignKey, Text, desc, or_
from sqlalchemy.orm import relationship, aliased, backref
from sqlalchemy.orm.exc import NoResultFound

from .source import Source, SourceType, Composite, CompositeComponent
from .statistic import StatisticJournal, StatisticName, StatisticJournalTimestamp
from ..support import Base
from ..utils import add
from ...commands.args import FORCE, mm
from ...diary.model import TYPE, DB, UNITS, VALUE
from ...lib import now, time_to_local_time
from ...lib.date import YMD
from ...lib.utils import inside_interval
from ...stats.names import KIT_ADDED, KIT_RETIRED, KIT_USED, ACTIVE_TIME, ACTIVE_DISTANCE, KM, S, _s, AGE, D

log = getLogger(__name__)

NAME = 'name'
GROUP = 'group'
ITEM = 'item'
ITEMS = _s(ITEM)
COMPONENT = 'component'
COMPONENTS = _s(COMPONENT)
MODEL = 'model'
MODELS = _s(MODEL)
STATISTICS = 'statistics'
N = 'n'
SUM = 'sum'
MEAN = 'mean'
MEDIAN = 'median'
ADDED = 'added'
EXPIRED = 'expired'

# statistics calculations
INDIVIDUAL = 'individual'
POPULATION = 'population'


'''
difficult design decisions here. 
  * too complex for integration with constants.
  * trade-off between simplicity and structure.  no type for top-level items, for example.
  * all parts are automatically given statistics for start and finish times
  * tie-in with activities to get active time / distance
in the end, what drove the design was the commands (see commands/kit.py) - trying to keep them as simple as possible.
unfortunately that pushed some extra complexity into the data model (eg to guarantee all names unique).
'''


def get_name(s, name, classes=None, require=False):
    # these cannot be parameter defaults because they're undefined at the top level
    classes = classes or (KitGroup, KitItem, KitComponent, KitModel)
    for cls in classes:
        # can be multiple models, in which case we return one 'at random'
        instance = s.query(cls).filter(cls.name == name).first()
        if instance:
            return instance
    if require:
        raise Exception(f'Cannot find "{name}"')


def assert_name_does_not_exist(s, name, use):
    instance = get_name(s, name)
    if instance and not isinstance(instance, use):
        raise Exception(f'The name "{name}" is already used for a {type(instance).SIMPLE_NAME}')


def expand_item(s, name, time):
    try:
        item = s.query(KitItem).filter(KitItem.name == name).one()
        start, finish = item.time_added(s), item.time_expired(s)
        if start <= time and (not finish or finish >= time):
            log.debug(f'Found {item}')
            yield item
            for model in s.query(KitModel).filter(KitModel.item == item).all():
                start, finish = model.time_added(s), model.time_expired(s)
                if start <= time and (not finish or finish >= time):
                    log.debug(f'Found {model}')
                    yield model
                else:
                    log.debug(f'Outside time {start} <= {time} <= {finish}')
        else:
            log.debug(f'Outside time {start} <= {time} <= {finish}')
    except NoResultFound as e:
        log.error(e)
        raise Exception(f'Kit item {name} not defined')


class StatisticsMixin:
    '''
    support for reading and writing statistics related to a kit class.
    '''

    def _base_statistic_query(self, s, statistic, *sources, owner=None):
        sources = (self,) + sources
        subq = s.query(Composite.id.label('composite_id'))
        for source in sources:
            cc = aliased(CompositeComponent)
            subq = subq.join(cc, Composite.id == cc.output_source_id).filter(cc.input_source == source)
        subq = subq.subquery()
        q = s.query(StatisticJournal). \
            join(StatisticName). \
            outerjoin(subq, subq.c.composite_id == StatisticJournal.source_id). \
            filter(StatisticName.name == statistic,
                   StatisticName.constraint == None)
        if len(sources) == 1:
            q = q.filter(or_(StatisticJournal.source == self, subq.c.composite_id != None))
        else:
            q = q.filter(subq.c.composite_id != None)
        if owner:
            q = q.filter(StatisticName.owner == owner)
        return q

    def _base_use_query(self, s, statistic):
        cc1, cc2 = aliased(CompositeComponent), aliased(CompositeComponent)
        sourceq = s.query(cc1.input_source_id). \
            join(Composite, Composite.id == cc1.output_source_id). \
            join(cc2, cc2.output_source_id == Composite.id). \
            filter(cc2.input_source == self,
                   cc1.input_source != self).subquery()
        return s.query(StatisticJournal). \
            join(StatisticName). \
            filter(StatisticName.name == statistic,
                   StatisticJournal.source_id.in_(sourceq))

    def _get_statistic(self, s, statistic, *sources, owner=None):
        return self._base_statistic_query(s, statistic, *sources, owner=owner).one_or_none()

    def _get_statistics(self, s, statistic, *sources, owner=None):
        return self._base_statistic_query(s, statistic, *sources, owner=owner).all()

    def _remove_statistic(self, s, statistic, *sources, owner=None):
        # cannot delete directly with join
        for instance in self._get_statistics(s, statistic, *sources, owner=owner):
            s.delete(instance)

    def _add_timestamp(self, s, statistic, time, source=None, owner=None):
        log.debug(f'Add timestamp for {statistic} at {time} with source {source}')
        # if source is given it is in addition to self
        if source:
            source = Composite.create(s, source, self)
            log.debug(f'Composite {source}')
        else:
            source = self
        owner = owner or self
        return StatisticJournalTimestamp.add(s, statistic, None, None, owner, None, source, time,
                                             description='A timestamp for tracking kit use.')

    def time_added(self, s):
        return self._get_statistic(s, KIT_ADDED).time

    def time_expired(self, s):
        try:
            return self._get_statistic(s, KIT_RETIRED).time
        except AttributeError:
            return None

    def add_use(self, s, time, source=None, owner=None):
        self._add_timestamp(s, KIT_USED, time, source=source, owner=owner)

    def active_times(self, s):
        return self._base_use_query(s, ACTIVE_TIME).all()

    def active_distances(self, s):
        return self._base_use_query(s, ACTIVE_DISTANCE).all()

    def lifetime(self, s):
        added, expired = self.time_added(s), self.time_expired(s)
        expired = expired or now()
        return expired - added

    def _add_individual_statistics(self, s, model):
        model_statistics = []
        self._calculate_individual_statistics(model_statistics, ACTIVE_DISTANCE, self.active_distances(s), KM)
        self._calculate_individual_statistics(model_statistics, ACTIVE_TIME, self.active_times(s), S)
        expire = self.time_expired(s) or now()
        model_statistics.append({NAME: AGE, N: 1, SUM: (expire - self.time_added(s)).days, UNITS: D})
        model[STATISTICS] = model_statistics

    def _calculate_individual_statistics(self, model_statistics, name, values, units):
        n = len(values)
        if n:
            values = [value.value for value in values]
            total = sum(values)
            # had mean and median, but they were pointless
            model_statistics.append({NAME: name, N: n, SUM: total, UNITS: units})


class ModelMixin:

    @staticmethod
    def fmt_time(time):
        if time:
            return time_to_local_time(time, fmt=YMD)
        else:
            return None

    def to_model(self, s, depth=0, statistics=None, time=None, own_models=True):
        model = {TYPE: self.SIMPLE_NAME, DB: self.id, NAME: self.name}
        try:
            model.update({ADDED: self.fmt_time(self.time_added(s)),
                          EXPIRED: self.fmt_time(self.time_expired(s))})
        except AttributeError:
            pass  # not a subclass of statistics mixin
        if depth > 0:
            self._add_children(s, model, depth=depth-1, statistics=statistics, time=time, own_models=own_models)
        try:
            if statistics == INDIVIDUAL:
                self._add_individual_statistics(s, model)
        except AttributeError:
            log.debug(f'No {statistics} statistics for {self.SIMPLE_NAME} {self.name}')
        return model


class KitGroup(ModelMixin, Base):
    '''
    top level group for kit (bike, shoe, etc)
    '''

    __tablename__ = 'kit_group'
    SIMPLE_NAME = 'group'

    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False, index=True, unique=True)

    @classmethod
    def get_or_add(cls, s, name, force=False):
        try:
            return s.query(KitGroup).filter(KitGroup.name == name).one()
        except NoResultFound:
            assert_name_does_not_exist(s, name, KitGroup)
            if force:
                log.warning(f'Forcing creation of new group ({name})')
                return add(s, KitGroup(name=name))
            else:
                groups = \
                    s.query(KitGroup).order_by(KitGroup.name).all()
                if groups:
                    log.info('Existing groups:')
                    for existing in groups:
                        log.info(f'  {existing.name}')
                    raise Exception(f'Give an existing group, or specify {mm(FORCE)} to create a new group ({name})')
                else:
                    raise Exception(f'Specify {mm(FORCE)} to create a new group ({name})')

    def _add_children(self, s, model, depth=0, statistics=None, time=None, own_models=True):
        model[ITEMS] = [item.to_model(s, depth=depth, statistics=statistics, 
                                      time=time, own_models=own_models)
                        for item in self.items
                        if time is None or inside_interval(item.time_added(s), time, item.time_expired(s))]

    def __str__(self):
        return f'KitGroup "{self.name}"'


class KitItem(ModelMixin, StatisticsMixin, Source):
    '''
    an individual kit item (a particular bike, a particular shoe, etc)
    '''

    __tablename__ = 'kit_item'
    SIMPLE_NAME = 'item'

    id = Column(Integer, ForeignKey('source.id', ondelete='cascade'), primary_key=True)
    group_id = Column(Integer, ForeignKey('kit_group.id', ondelete='cascade'), nullable=False, index=True)
    group = relationship('KitGroup', backref=backref('items', passive_deletes=True))
    name = Column(Text, nullable=False, index=True, unique=True)

    __mapper_args__ = {
        'polymorphic_identity': SourceType.ITEM
    }

    @classmethod
    def add(cls, s, group, name, date):
        # don't rely on unique index to catch duplicates because that's not triggered until commit
        if s.query(KitItem).filter(KitItem.name == name).count():
            raise Exception(f'Item {name} of group {group.name} already exists')
        else:
            assert_name_does_not_exist(s, name, KitItem)
            item = add(s, KitItem(group=group, name=name))
            item._add_statistics(s, date)
            return item

    def _add_statistics(self, s, time):
        self._add_timestamp(s, KIT_ADDED, time)

    @classmethod
    def get(cls, s, name):
        try:
            return s.query(KitItem).filter(KitItem.name == name).one()
        except NoResultFound:
            raise Exception(f'Item {name} does not exist')

    @property
    def components(self):
        components = defaultdict(list)
        for model in self.models:
            components[model.component].append(model)
        return components

    def finish(self, s, date, force):
        if self.time_expired(s):
            if force:
                self._remove_statistic(s, KIT_RETIRED, owner=self)
            else:
                raise Exception(f'Item {self.name} is already retired')
        self._add_timestamp(s, KIT_RETIRED, date)

    def delete(self, s):
        s.delete(self)
        Composite.clean(s)

    def _add_children(self, s, model, depth=0, statistics=None, time=None, own_models=True):
        model[COMPONENTS] = [component.to_model(s, depth=depth, statistics=statistics, 
                                                time=time, own_models=own_models)
                             for component in self.components]
        model[MODELS] = [model.to_model(s, depth=depth, statistics=statistics, 
                                        time=time, own_models=own_models)
                         for model in self.models
                         if time is None or inside_interval(model.time_added(s), time, model.time_expired(s))]

    def to_model(self, s, depth=0, statistics=None, time=None, own_models=True):
        model = super().to_model(s, depth=depth, statistics=statistics, time=time)
        model_ids = set(model.id for model in self.models)
        if own_models and COMPONENTS in model:
            for component in model[COMPONENTS]:
                # restrict component's models to subset of own models
                component[MODELS] = [model for model in component[MODELS] if model[DB] in model_ids]
        return model

    def __str__(self):
        return f'KitItem "{self.name}"'


class KitComponent(ModelMixin, Base):
    '''
    the kind of thing that a kit item is made of (bike wheel, shoe laces, etc)
    '''

    __tablename__ = 'kit_component'
    SIMPLE_NAME = 'component'

    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False, index=True)

    @classmethod
    def get(cls, s, name, require=True):
        instance = s.query(KitComponent).filter(KitComponent.name == name).one_or_none()
        if require and not instance:
            raise Exception(f'Component {name} does not exist')
        return instance

    @classmethod
    def get_or_add(cls, s, name, force):
        try:
            return s.query(KitComponent).filter(KitComponent.name == name).one()
        except NoResultFound:
            assert_name_does_not_exist(s, name, KitComponent)
            if force:
                log.warning(f'Forcing creation of new component ({name})')
                return add(s, KitComponent(name=name))
            else:
                components = s.query(KitComponent).order_by(KitComponent.name).all()
                if components:
                    log.info('Existing components:')
                    for existing in components:
                        log.info(f'  {existing.name}')
                    raise Exception(f'Give an existing component, or specify {mm(FORCE)} to create a new one ({name})')
                else:
                    raise Exception(f'Specify {mm(FORCE)} to create a new component ({name})')

    def delete_if_unused(self, s):
        s.refresh(self)  # make sure deleted models are no longer present
        if not self.models:
            s.delete(self)

    def _add_children(self, s, model, depth=0, statistics=None, time=None, own_models=True):
        model_statistics = INDIVIDUAL if statistics == POPULATION else statistics
        model[MODELS] = [model.to_model(s, depth=depth, statistics=model_statistics,
                                        time=time, own_models=own_models)
                         for model in self.models
                         if time is None or inside_interval(model.time_added(s), time, model.time_expired(s))]
        if statistics == POPULATION:
            self._add_population_statistics(model)

    def to_model(self, s, depth=0, statistics=None, time=None, own_models=True):
        # force all time, since this is constrained via the item if needed and, if not, helps prompts
        return super().to_model(s, depth=depth, statistics=statistics, time=time if statistics else None)

    def _add_population_statistics(self, model):
        # replace KitModel instances with populations.  so this doesn't just rewrite the statistics,
        # it can actually rewrite the model
        population_models = {}
        for instance in model[MODELS]:
            name = instance[NAME]
            if name not in population_models:
                population_models[name] = instance
                for statistic in instance[STATISTICS]:
                    statistic[N] = 1
                    statistic[MEAN] = statistic[SUM]
                    del statistic[SUM]
                del instance[DB]
                del instance[ADDED]
                del instance[EXPIRED]
            else:
                population = population_models[NAME]
                for instance_statistic in instance[STATISTICS]:
                    population_statistic = population[STATISTICS]. \
                        filter(lambda statistic: statistic[NAME] == instance_statistic[NAME])[0]
                    population_statistic[N] += 1
                    population_statistic[MEAN] += instance_statistic[SUM]
        model[MODELS] = []
        for name in population_models:
            population = population_models[name]
            for statistic in population[STATISTICS]:
                statistic[MEAN] /= statistic[N]
            model[MODELS].append(population)

    def __str__(self):
        return f'KitComponent "{self.name}"'


class KitModel(ModelMixin, StatisticsMixin, Source):
    '''
    a particular piece of a kit item (a particular bike wheel, a particular set of laces, etc).
    '''

    __tablename__ = 'kit_model'
    SIMPLE_NAME = 'model'

    id = Column(Integer, ForeignKey('source.id', ondelete='cascade'), primary_key=True)
    item_id = Column(Integer, ForeignKey('kit_item.id', ondelete='cascade'), nullable=False, index=True)
    item = relationship('KitItem', foreign_keys=[item_id], backref=backref('models', passive_deletes=True))
    component_id = Column(Integer, ForeignKey('kit_component.id', ondelete='cascade'), nullable=False, index=True)
    component = relationship('KitComponent', backref=backref('models', passive_deletes=True))
    name = Column(Text, nullable=False, index=True)

    __mapper_args__ = {
        'polymorphic_identity': SourceType.MODEL
    }

    @classmethod
    def add(cls, s, item, component, name, time):
        cls._reject_duplicate(s, item, component, name, time)
        model = cls._add_instance(s, item, component, name)
        model._add_statistics(s, time)
        return model

    @classmethod
    def _reject_duplicate(cls, s, item, component, name, time):
        if s.query(StatisticJournal). \
                join(StatisticName). \
                join(KitModel, KitModel.id == StatisticJournal.source_id). \
                filter(StatisticName.name == KIT_ADDED,
                       StatisticJournal.time == time,
                       KitModel.name == name,
                       KitModel.component == component,
                       KitModel.item == item).count():
            raise Exception(f'This part already exists at this date')

    @classmethod
    def _add_instance(cls, s, item, component, name):
        # TODO - restrict name to a particular component
        if not s.query(KitModel).filter(KitModel.name == name).count():
            assert_name_does_not_exist(s, name, KitModel)
            log.warning(f'Model {name} does not match any previous entries')
        return add(s, KitModel(item=item, component=component, name=name))

    def _add_statistics(self, s, time):
        self._add_timestamp(s, KIT_ADDED, time)
        before = self.before(s, time)
        after = self.after(s, time)
        if before:
            before_expiry = before.time_expired(s)
            if before_expiry and before_expiry > time:
                before._remove_statistic(s, KIT_RETIRED)
                before_expiry = None
            if not before_expiry:
                before._add_timestamp(s, KIT_RETIRED, time)
                log.info(f'Retired previous {self.component.name} ({before.name})')
        if after:
            after_added = after.time_added(s)
            self._add_timestamp(s, KIT_RETIRED, after_added)
            log.info(f'Retired new {self.component.name} ({self.name})')

    @classmethod
    def get_all_at(cls, s, item, time):
        beforeq = s.query(StatisticJournalTimestamp.source_id, StatisticJournalTimestamp.time). \
            join(StatisticName). \
            filter(StatisticName.name == KIT_ADDED).subquery()
        afterq = s.query(StatisticJournalTimestamp.source_id, StatisticJournalTimestamp.time). \
            join(StatisticName). \
            filter(StatisticName.name == KIT_RETIRED).subquery()
        return s.query(KitModel). \
            join(beforeq, beforeq.c.source_id == KitModel.id). \
            outerjoin(afterq, afterq.c.source_id == KitModel.id). \
            filter(KitModel.item == item,
                   beforeq.c.time <= time,
                   or_(afterq.c.time >= time, afterq.c.time == None)).all()

    @classmethod
    def get_all(cls, s, item, component):
        return s.query(KitModel). \
            filter(KitModel.item == item,
                   KitModel.component == component).all()

    @classmethod
    def get(cls, s, item, component, name, time, require=True):
        # if time is None, any appropriate model is returned
        q = s.query(KitModel)
        if time:
            beforeq = s.query(StatisticJournalTimestamp.source_id, StatisticJournalTimestamp.time). \
                join(StatisticName). \
                filter(StatisticName.name == KIT_ADDED).subquery()
            afterq = s.query(StatisticJournalTimestamp.source_id, StatisticJournalTimestamp.time). \
                join(StatisticName). \
                filter(StatisticName.name == KIT_RETIRED).subquery()
            q = q.join(beforeq, beforeq.c.source_id == KitModel.id). \
                outerjoin(afterq, afterq.c.source_id == KitModel.id). \
                filter(beforeq.c.time <= time,
                       or_(afterq.c.time >= time, afterq.c.time == None))
        instance = q.filter(KitModel.item == item,
                            KitModel.component == component,
                            KitModel.name == name).first()
        if not instance and require:
            raise Exception(f'Model {name} does not exist')
        return instance

    def _base_sibling_query(self, s, statistic):
        return s.query(KitModel). \
            join(StatisticJournal, StatisticJournal.source_id == KitModel.id). \
            join(StatisticName). \
            join(KitComponent, KitComponent.id == KitModel.component_id). \
            join(KitItem, KitItem.id == KitModel.item_id). \
            filter(StatisticName.name == statistic,
                   KitComponent.name == self.component.name,
                   KitItem.name == self.item.name)

    def before(self, s, time=None):
        if not time:
            time = self.time_added(s)
        return self._base_sibling_query(s, KIT_ADDED).filter(StatisticJournal.time < time). \
            order_by(desc(StatisticJournal.time)).first()

    def after(self, s, time=None):
        if not time:
            time = self.time_added(s)
        return self._base_sibling_query(s, KIT_ADDED).filter(StatisticJournal.time > time). \
            order_by(StatisticJournal.time).first()

    def undo(self, s):
        time = self.time_added(s)
        s.delete(self)
        before = self.before(s, time)
        if before:
            before._remove_statistic(s, KIT_RETIRED)
            after = self.after(s, time)
            if after:
                before._add_timestamp(s, KIT_RETIRED, after.time_added(s))

    def time_range(self, s):
        return None, None

    def _add_children(self, s, model, depth=0, statistics=None, time=None, own_models=True):
        pass

    def __str__(self):
        return f'KitModel "{self.name}"'
