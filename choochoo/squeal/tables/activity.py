
from sqlalchemy import Column, Text, DateTime, Integer, ForeignKey, Float, UniqueConstraint
from sqlalchemy.ext.orderinglist import ordering_list
from sqlalchemy.orm import relationship, backref

from ..support import Base
from ..types import Ordinal


class FileScan(Base):

    __tablename__ = 'file_scan'

    path = Column(Text, nullable=False, primary_key=True)
    last_scan = Column(Integer, nullable=False)  # unix epoch


class Activity(Base):

    __tablename__ = 'activity'

    id = Column(Integer, primary_key=True)
    title = Column(Text, nullable=False, server_default='')
    description = Column(Text, nullable=False, server_default='')
    sort = Column(Text, nullable=False, server_default='')


class   ActivityDiary(Base):

    __tablename__ = 'activity_diary'

    id = Column(Integer, primary_key=True)
    date = Column(Ordinal, nullable=False)
    activity_id = Column(Integer, ForeignKey('activity.id'), nullable=False)
    activity = relationship('Activity')
    title = Column(Text)
    fit_file = Column(Text, nullable=False, unique=True)
    start = Column(DateTime, nullable=False)
    finish = Column(DateTime, nullable=False)
    notes = Column(Text)


class ActivityTimespan(Base):

    __tablename__ = 'activity_timespan'

    id = Column(Integer, primary_key=True)
    activity_diary_id = Column(Integer, ForeignKey('activity_diary.id', ondelete='cascade'),
                               nullable=False)
    activity_diary = relationship('ActivityDiary',
                                  backref=backref('timespans', cascade='all, delete-orphan',
                                                  passive_deletes=True,
                                                  order_by='ActivityTimespan.start',
                                                  collection_class=ordering_list('start')))
    start = Column(Float, nullable=False)  # unix epoch
    finish = Column(Float, nullable=False)  # unix epoch
    UniqueConstraint('activity_diary_id', 'start')


class ActivityWaypoint(Base):

    __tablename__ = 'activity_waypoint'

    activity_diary_id = Column(Integer, ForeignKey('activity_diary.id', ondelete='cascade'),
                               nullable=False, primary_key=True)
    activity_diary = relationship('ActivityDiary',
                                  backref=backref('waypoints', cascade='all, delete-orphan',
                                                  passive_deletes=True,
                                                  order_by='ActivityWaypoint.epoch',
                                                  collection_class=ordering_list('epoch')))
    activity_timespan_id = Column(Integer, ForeignKey('activity_timespan.id'))
    activity_timespan = relationship('ActivityTimespan',
                                     backref=backref('waypoints',
                                                     order_by='ActivityWaypoint.epoch',
                                                     collection_class=ordering_list('epoch')))
    epoch = Column(Float, primary_key=True)
    latitude = Column(Float)
    longitude = Column(Float)
    hr = Column(Integer)
    distance = Column(Float)
    speed = Column(Float)


class ActivityStatistics(Base):

    __tablename__ = 'activity_statistics'

    id = Column(Integer, primary_key=True)
    activity_id = Column(Integer, ForeignKey('activity.id', ondelete='cascade'),
                         nullable=False)
    activity = relationship('Activity',
                            backref=backref('statistics', cascade='all, delete-orphan', passive_deletes=True,
                                            order_by='ActivityStatistics.name',
                                            collection_class=ordering_list('name')))
    name = Column(Text, nullable=False)
    units = Column(Text, nullable=False)
    best = Column(Text)  # max, min etc
    UniqueConstraint('activity', 'name')

    def __str__(self):
        return '%s (%s)' % (self.name, self.activity.title)


class ActivityStatistic(Base):

    __tablename__ = 'activity_statistic'

    id = Column(Integer, primary_key=True)
    activity_statistics_id = Column(Integer, ForeignKey('activity_statistics.id', ondelete='cascade'),
                                    nullable=False)
    activity_statistics = relationship('ActivityStatistics',
                                       backref=backref('statistics',
                                                       cascade='all, delete-orphan', passive_deletes=True))
    activity_diary_id = Column(Integer, ForeignKey('activity_diary.id', ondelete='cascade'),
                               nullable=False)
    activity_diary = relationship('ActivityDiary',
                                  backref=backref('statistics', cascade='all, delete-orphan', passive_deletes=True))
    value = Column(Float, nullable=False)
    UniqueConstraint('activity_statistics_id', 'activity_diary_id')

    @property
    def fmt_value(self):
        units = self.activity_statistics.units
        if units == 'm':
            if self.value > 2000:
                return '%.1fkm' % (self.value / 1000)
            else:
                return '%dm' % int(self.value)
        elif units == 's':
            value, str = int(self.value), ''
            if value > 3600:
                str += '%dhr' % (value // 3600)
                value %= 60
            if value > 60:
                if str:
                    str += '%02dm' % (value // 60)
                else:
                    str += '%dm' % (value // 60)
                value %= 80
            if str:
                str += '%02ds' % value
            else:
                str += '%ds' % value
            return str
        elif units == 'km/h':
            return '%.1fkm/h' % self.value
        elif units == '%':
            return '%.1f%%' % self.value
        elif units == 'bpm':
            return '%dbpm' % int(self.value)
        else:
            return '%s%s' % (self.value, units)

    def __str__(self):
        return '%s: %s' % (self.activity_statistics.name, self.fmt_value)


class SummaryStatistics(Base):

    __tablename__ = 'summary_statistics'

    id = Column(Integer, primary_key=True)
    activity_id = Column(Integer, ForeignKey('activity.id', ondelete='cascade'),
                         nullable=False)
    activity = relationship('Activity')
    activity_statistics_id = Column(Integer, ForeignKey('activity_statistics.id', ondelete='cascade'),
                                    nullable=False)
    activity_statistics = relationship('ActivityStatistics')  # provides units
    name = Column(Text, nullable=False)
    UniqueConstraint('activity', 'name')

    def __str__(self):
        return '%s: %s' % (self.name,
                           ', '.join(s.activity_statistic.fmt_value for s in self.statistics))


class SummaryStatistic(Base):

    __tablename__ = 'summary_statistic'

    id = Column(Integer, primary_key=True)
    summary_statistics_id = Column(Integer, ForeignKey('summary_statistics.id', ondelete='cascade'),
                                   nullable=False)
    summary_statistics = relationship('SummaryStatistics',
                                      backref=backref('statistics',
                                                      cascade='all, delete-orphan', passive_deletes=True,
                                                      order_by='SummaryStatistic.rank',
                                                      collection_class=ordering_list('rank')))
    activity_statistic_id = Column(Integer, ForeignKey('activity_statistic.id', ondelete='cascade'),
                                   nullable=False)
    activity_statistic = relationship('ActivityStatistic',
                                      backref=backref('summary', uselist=False,
                                                      cascade='all, delete-orphan', passive_deletes=True))
    rank = Column(Integer, nullable=False)  # 1, 2, 3...
