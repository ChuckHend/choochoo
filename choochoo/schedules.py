
from urwid import Edit, Columns, Pile, CheckBox

from .log import make_log
from .repeating import DateOrdinals
from .squeal.binders import Binder
from .squeal.database import Database
from .squeal.schedule import Schedule, ScheduleType
from .uweird.calendar import TextDate
from .uweird.decorators import Indent
from .uweird.factory import Factory
from .uweird.focus import FocusWrap, MessageBar
from .uweird.tabs import TabList
from .uweird.widgets import DynamicContent, DividedPile, Menu, ColText, ColSpace, Nullable, SquareButton
from .widgets import App


class ScheduleWidget(FocusWrap):

    def __init__(self, log, tabs, bar, types):
        factory = Factory(tabs, bar)
        self.type_id = factory(Menu('Type: ', types))
        self.title = factory(Edit('Title: '))
        self.repeat = factory(Edit('Repeat: '))
        self.start = factory(Nullable('Open', lambda state: TextDate(log, date=state)))
        self.finish = factory(Nullable('Open', lambda state: TextDate(log, date=state)))
        self.description = factory(Edit('Description: ', multiline=True))
        self.sort = factory(Edit('Sort: '))
        self.has_notes = factory(CheckBox("Notes? "))
        delete = SquareButton('Delete')
        reset = SquareButton('Reset')
        add_child = SquareButton('Add')
        body = [Columns([('weight', 1, self.type_id), ColText('  '), ('weight', 3, self.title)]),
                Columns([self.repeat, ColText('  '), self.start, ColText('  '), self.finish]),
                self.description,
                Columns([self.sort, ColText('  '), self.has_notes, ColSpace(), (7, add_child), (10, delete), (9, reset)])]
        super().__init__(Pile(body))


class SchedulesEditor(FocusWrap):

    def __init__(self, log, session, bar, schedules, ordinals, types):
        self.__log = log
        self.__session = session
        self.__bar = bar
        self.__ordinals = ordinals
        self.__types = types
        tabs = TabList()
        body = []
        for schedule in sorted(schedules):
            body.append(self.__nested(schedule, tabs))
        add = SquareButton('Add')
        body.append(Columns([(7, add), ColSpace()]))
        super().__init__(DividedPile(body))

    def __nested(self, schedule, tabs):
        widget = ScheduleWidget(self.__log, tabs, self.__bar, self.__types)
        Binder(self.__log, self.__session, widget, instance=schedule)
        children = []
        for child in sorted(schedule.children):
            if child.at_location(self.__ordinals):
                children.append(self.__nested(child, tabs))
        if children:
            widget = DividedPile([widget, Indent(DividedPile(children), width=2)])
        return widget


class SchedulesFilter(DynamicContent):

    # two-stage approach here
    # outer filter commits / reads from the database and redisplays the tree
    # inner editor works only within the session

    def __init__(self, log, session, bar):
        self.__tabs = TabList()
        # factory = Factory(self.__tabs, bar)
        self.__types = dict((type.id, type.name) for type in session.query(ScheduleType).all())
        self.type = Nullable('Any type', lambda state: Menu('', self.__types, state=state))
        self.date = Nullable('Any date', lambda state: TextDate(log, date=state))
        super().__init__(log, session, bar)

    def _make(self):
        query = self._session.query(Schedule).filter(Schedule.parent_id == None)
        type_id = self.type.state
        if type_id is not None:
            query = query.filter(Schedule.type_id == type_id)
        root_schedules = list(query.all())
        date = self.date.state
        if date is not None:
            date = DateOrdinals(date)
            root_schedules = [schedule for schedule in root_schedules if schedule.at_location(date)]
        # todo - tabs
        apply = SquareButton('Apply')
        discard = SquareButton('Discard')
        editor = SchedulesEditor(self._log, self._session, self._bar, root_schedules, date, self.__types)
        body = [Columns([ColText('Filter: '),
                         (18, self.date),
                         ColText(' '),
                         self.type,
                         ColSpace(),
                         (9, apply),
                         (11, discard),
                         ]),
                editor]
        return DividedPile(body), self.__tabs


class ScheduleApp(App):

    def __init__(self, log, session, bar):
        self.__session = session
        tabs = TabList()
        self.injuries = tabs.append(SchedulesFilter(log, session, bar))
        super().__init__(log, 'Schedules', bar, self.injuries, tabs, session)


def main(args):
    log = make_log(args)
    session = Database(args, log).session()
    ScheduleApp(log, session, MessageBar()).run()