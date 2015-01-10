import json
from itertools import count
from datetime import date

from sqlalchemy import MetaData
from sqlalchemy.schema import Table, Column
from sqlalchemy.types import Unicode, Integer, Date, Float
from sqlalchemy.sql.expression import select, func, extract

from openspending.core import db
from openspending.lib.util import cache_hash
from openspending.model.visitor import ModelVisitor
from openspending.model.common import json_default


TYPES = {
    'string': Unicode,
    'integer': Integer,
    'float': Float,
    'date': Date
}


class FactTableMapping(ModelVisitor):
    """ The mapping helps to establish a link between the physical
    columns on the fact table and the dimensions, measures etc. of
    the model. """

    def __init__(self, alias, fields, model):
        self.alias = alias
        self.fields = fields
        self.model = model
        self.columns = {}

    def apply(self):
        self.visit(self.model)

    def visit_attribute(self, attribute):
        if attribute.column not in self.alias.columns:
            return
        col = self.alias.c[attribute.column]
        self.columns[attribute.path] = col

    def visit_date_dimension(self, dimension):
        if dimension.column not in self.alias.columns:
            return
        col = self.alias.c[dimension.column]
        name_attr = dimension['name']
        self.columns[name_attr.path] = col.label(name_attr.column)
        
        field_type = self.fields.get(dimension.column).get('type')
        if field_type == 'date':
            for a in ['year', 'quarter', 'month', 'week', 'day']:
                attr = dimension[a]
                self.columns[attr.path] = extract(col, a).label(attr.column, a)
        elif field_type == 'integer':
            year_attr = dimension['year']
            self.columns[year_attr.path] = col.label(year_attr.column)

    def unpack_entry(self, row):
        """ Convert a database-returned row into a nested and mapped
        fact representation. """
        row = dict(row.items())
        result = {'id': row.get('_id')}
        for axis in self.model.axes:
            if hasattr(axis, 'attributes'):
                value = {}
                for attr in axis.attributes:
                    value[attr.name] = row.get(attr.column)
            else:
                value = row.get(axis.column)
            result[axis.name] = value
        return result


class FactTable(object):
    """ The ``FactTable`` serves as a controller object for
    a given ``Model``, handling the creation, filling and migration
    of the table schema associated with the dataset. """
    
    def __init__(self, dataset):
        self.dataset = dataset
        
        self.bind = db.engine
        self.meta = MetaData()
        self.meta.bind = self.bind
        self._table = None
        
    @property
    def table(self):
        """ Generate an appropriate table representation to mirror the
        fields known for this table. """
        if self._table is None:
            name = '%s__facts' % self.dataset.name
            self._table = Table(name, self.meta)
            id_col = Column('_id', Unicode(42), primary_key=True)
            self._table.append_column(id_col)
            json_col = Column('_json', Unicode())
            self._table.append_column(json_col)
            self._fields_columns(self._table)
        return self._table

    @property
    def alias(self):
        """ An alias used for queries. """
        if not hasattr(self, '_alias'):
            self._alias = self.table.alias('entry')
        return self._alias

    @property
    def mapping(self):
        if not hasattr(self, '_mapping'):
            self._mapping = FactTableMapping(self.alias, self.dataset.fields,
                                             self.dataset.model)
            self._mapping.apply()
        return self._mapping
        
    @property
    def exists(self):
        return db.engine.has_table(self.table.name)

    def _fields_columns(self, table):
        """ Transform the (auto-detected) fields into a set of column
        specifications. """

        for name, field in self.dataset.fields.items():
            data_type = TYPES.get(field.get('type'), Unicode)
            col = Column(name, data_type, nullable=True)
            table.append_column(col)

    def load_iter(self, iterable, chunk_size=1000):
        """ Bulk load all the data in an artifact to a matching database
        table. """
        chunk = []
        conn = self.bind.connect()
        tx = conn.begin()
        try:
            for record in iterable:
                chunk.append(self._expand_record(record))
                if len(chunk) >= chunk_size:
                    stmt = self.table.insert(chunk)
                    conn.execute(stmt)
                    chunk = []

            if len(chunk):
                stmt = self.table.insert(chunk)
                conn.execute(stmt)
            tx.commit()
        except:
            tx.rollback()
            raise

    def _expand_record(self, record):
        """ Transform an incoming record into a form that matches the
        fields schema. """
        record['_id'] = cache_hash(record)
        record['_json'] = json.dumps(record, default=json_default)
        return record

    def create(self):
        """ Create the fact table if it does not exist. """
        if not self.exists:
            self.table.create(self.bind)

    def drop(self):
        """ Drop the fact table if it does exist. """
        if self.exists:
            self.table.drop()
        self._table = None

    def num_entries(self):
        """ Get the number of facts that are currently loaded. """
        if not self.exists:
            return 0
        rp = self.bind.execute(self.table.count())
        return rp.fetchone()[0]

    def dimension_members(self, dimension, offset=0, limit=None):
        prefix = dimension.name + '.'
        selects = []
        for path, col in self.mapping.columns.items():
            if path == dimension.name or path.startswith(prefix):
                selects.append(col)
        order_by = [s.asc() for s in selects]
        for entry in self.entries(order_by=order_by, selects=selects,
                                  distinct=True, offset=offset, limit=limit):
            yield entry.get(dimension.name)

    def entries(self, conditions="1=1", order_by=None, limit=None,
                selects=[], distinct=False, offset=0, step=10000):
        """ Generate a fully denormalized view of the entries on this
        table. This view is nested so that each dimension will be a hash
        of its attributes. """
        if not self.exists:
            return

        if not selects:
            selects = [self.alias.c._id] + self.mapping.columns.values()

            # enforce stable sorting:
            if order_by is None:
                order_by = [self.alias.c._id.asc()]
        
        assert order_by is not None

        for i in count():
            qoffset = offset + (step * i)
            qlimit = step
            if limit is not None:
                qlimit = min(limit - (step * i), step)
            if qlimit <= 0:
                break

            query = select(selects, conditions, [], order_by=order_by,
                           distinct=distinct, limit=qlimit, offset=qoffset)
            rp = self.bind.execute(query)
            first_row = True
            while True:
                row = rp.fetchone()
                if row is None:
                    if first_row:
                        return
                    break
                first_row = False
                yield self.mapping.unpack_entry(row)

    def timerange(self):
        """
        Get the timerange of the dataset (based on the time attribute).
        Returns a tuple of (first timestamp, last timestamp) where timestamp
        is a datetime object
        """
        if not self.exists or not self.dataset.model.exists:
            return (None, None)
    
        # Get the time column
        time = self.dataset.model['time']
        time = time.alias.c[time.column]
        # We use SQL's min and max functions to get the timestamps
        query = db.session.query(func.min(time), func.max(time))
        # We just need one result to get min and max time
        
        def convert(d):
            if isinstance(d, date):
                return d
            if isinstance(d, int):
                return date(d, 1, 1)
        return [convert(d) for d in query.one()]

    def __repr__(self):
        return "<FactTable(%r)>" % (self.dataset)
