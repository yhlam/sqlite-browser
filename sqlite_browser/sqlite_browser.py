#!/usr/bin/env python

import chardet
import datetime
import math
import optparse
import os
import sys
import threading
import time
import webbrowser
from collections import namedtuple, OrderedDict
from functools import wraps

# Py3k compat.
if sys.version_info[0] == 3:
    basestring = str
    unicode = str
    from io import StringIO
else:
    from StringIO import StringIO

from flask import (
    Flask, abort, escape, flash, make_response, redirect, render_template,
    request, url_for)
from peewee import *
from playhouse.dataset import DataSet
from playhouse.migrate import migrate


CUR_DIR = os.path.realpath(os.path.dirname(__file__))
DEBUG = False
MAX_RESULT_SIZE = 1000
ROWS_PER_PAGE = 50
SECRET_KEY = 'sqlite-database-browser-0.1.0'

app = Flask(
    __name__,
    static_folder=os.path.join(CUR_DIR, 'static'),
    template_folder=os.path.join(CUR_DIR, 'templates'))
app.config.from_object(__name__)
dataset = None
migrator = None

#
# Database metadata objects.
#

IndexMetadata = namedtuple(
    'IndexMetadata',
    ('name', 'sql', 'columns', 'unique'))

ColumnMetadata = namedtuple(
    'ColumnMetadata',
    ('name', 'data_type', 'null', 'primary_key'))

ForeignKeyMetadata = namedtuple(
    'ForeignKeyMetadata',
    ('column', 'dest_table', 'dest_column'))

TriggerMetadata = namedtuple('TriggerMetadata', ('name', 'sql'))

ViewMetadata = namedtuple('ViewMetadata', ('name', 'sql'))

#
# Database helpers.
#

def get_indexes(table):
    # Retrieve all indexes and their associated SQL.
    cursor = dataset.query(
        'SELECT name, sql FROM sqlite_master '
        'WHERE tbl_name = ? AND type = ? '
        'ORDER BY name ASC',
        [table, 'index'])
    index_to_sql = dict(cursor.fetchall())

    # Determine which indexes have a unique constraint.
    unique_indexes = set()
    cursor = dataset.query('PRAGMA index_list("%s")' % table)
    for _, name, is_unique in cursor.fetchall():
        if is_unique:
            unique_indexes.add(name)

    # Retrieve the indexed columns.
    index_columns = {}
    for index_name in index_to_sql:
        cursor = dataset.query('PRAGMA index_info("%s")' % index_name)
        index_columns[index_name] = [row[2] for row in cursor.fetchall()]

    return [
        IndexMetadata(
            name,
            index_to_sql[name],
            index_columns[name],
            name in unique_indexes)
        for name in sorted(index_to_sql)]

def get_columns(table):
    #('name', 'data_type', 'null', 'primary_key'))
    cursor = dataset.query('PRAGMA table_info("%s")' % table)
    return [
        ColumnMetadata(
            row[1],
            row[2],
            not row[3],
            row[5])
        for row in cursor.fetchall()]

def get_foreign_keys(table):
    cursor = dataset.query('PRAGMA foreign_key_list("%s")' % table)
    return [
        ForeignKeyMetadata(row[3], row[2], row[4])
        for row in cursor.fetchall()]

def get_triggers(table):
    cursor = dataset.query(
        'SELECT name, sql FROM sqlite_master '
        'WHERE type = ? AND tbl_name = ?',
        ('trigger', table))
    return [TriggerMetadata(*row) for row in cursor.fetchall()]

def get_views():
    cursor = dataset.query(
        'SELECT name, sql FROM sqlite_master '
        'WHERE type = ?',
        ('view',))
    return [ViewMetadata(*row) for row in cursor.fetchall()]

def get_virtual_tables():
    cursor = dataset.query(
        'SELECT name FROM sqlite_master '
        'WHERE type = ? AND sql LIKE ? '
        'ORDER BY name',
        ('table', 'CREATE VIRTUAL TABLE%'))
    return set([row[0] for row in cursor.fetchall()])

def get_corollary_virtual_tables():
    virtual_tables = get_virtual_tables()
    suffixes = ['content', 'docsize', 'segdir', 'segments', 'stat']
    return set(
        '%s_%s' % (virtual_table, suffix) for suffix in suffixes
        for virtual_table in virtual_tables)

#
# Flask views.
#

@app.route('/')
def index():
    return render_template('index.html')

def require_table(fn):
    @wraps(fn)
    def inner(table, *args, **kwargs):
        if table not in dataset.tables:
            abort(404)
        return fn(table, *args, **kwargs)
    return inner

@app.route('/create-table/', methods=['POST'])
def table_create():
    table = (request.form.get('table_name') or '').strip()
    if not table:
        flash('Table name is required.', 'danger')
        return redirect(request.form.get('redirect') or url_for('index'))

    dataset[table]
    return redirect(url_for('table_import', table=table))

@app.route('/<table>/')
@require_table
def table_structure(table):
    ds_table = dataset[table]
    model_class = ds_table.model_class

    table_sql = dataset.query(
        'SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type = ?',
        [table, 'table']).fetchone()[0]

    return render_template(
        'table_structure.html',
        columns=get_columns(table),
        ds_table=ds_table,
        foreign_keys=get_foreign_keys(table),
        indexes=get_indexes(table),
        model_class=model_class,
        table=table,
        table_sql=table_sql,
        triggers=get_triggers(table))

def get_request_data():
    if request.method == 'POST':
        return request.form
    return request.args

@app.route('/<table>/add-column/', methods=['GET', 'POST'])
@require_table
def add_column(table):
    column_mapping = OrderedDict((
        ('VARCHAR', CharField),
        ('TEXT', TextField),
        ('INTEGER', IntegerField),
        ('REAL', FloatField),
        ('BOOL', BooleanField),
        ('BLOB', BlobField),
        ('DATETIME', DateTimeField),
        ('DATE', DateField),
        ('TIME', TimeField),
        ('DECIMAL', DecimalField)))

    request_data = get_request_data()
    col_type = request_data.get('type')
    name = request_data.get('name', '')

    if request.method == 'POST':
        if name and col_type in column_mapping:
            migrate(
                migrator.add_column(
                    table,
                    name,
                    column_mapping[col_type](null=True)))
            flash('Column "%s" was added successfully!' % name, 'success')
            return redirect(url_for('table_structure', table=table))
        else:
            flash('Name and column type are required.', 'danger')

    return render_template(
        'add_column.html',
        col_type=col_type,
        column_mapping=column_mapping,
        name=name,
        table=table)

@app.route('/<table>/drop-column/', methods=['GET', 'POST'])
@require_table
def drop_column(table):
    request_data = get_request_data()
    name = request_data.get('name', '')
    columns = get_columns(table)
    column_names = [column.name for column in columns]

    if request.method == 'POST':
        if name in column_names:
            migrate(migrator.drop_column(table, name))
            flash('Column "%s" was dropped successfully!' % name, 'success')
            return redirect(url_for('table_structure', table=table))
        else:
            flash('Name is required.', 'danger')

    return render_template(
        'drop_column.html',
        columns=columns,
        column_names=column_names,
        name=name,
        table=table)

@app.route('/<table>/rename-column/', methods=['GET', 'POST'])
@require_table
def rename_column(table):
    request_data = get_request_data()
    rename = request_data.get('rename', '')
    rename_to = request_data.get('rename_to', '')

    columns = get_columns(table)
    column_names = [column.name for column in columns]

    if request.method == 'POST':
        if (rename in column_names) and (rename_to not in column_names):
            migrate(migrator.rename_column(table, rename, rename_to))
            flash('Column "%s" was renamed successfully!' % rename, 'success')
            return redirect(url_for('table_structure', table=table))
        else:
            flash('Column name is required and cannot conflict with an '
                  'existing column\'s name.', 'danger')

    return render_template(
        'rename_column.html',
        columns=columns,
        column_names=column_names,
        rename=rename,
        rename_to=rename_to,
        table=table)

@app.route('/<table>/add-index/', methods=['GET', 'POST'])
@require_table
def add_index(table):
    request_data = get_request_data()
    indexed_columns = request_data.getlist('indexed_columns')
    unique = bool(request_data.get('unique'))

    columns = get_columns(table)
    column_names = [column.name for column in columns]

    if request.method == 'POST':
        if indexed_columns:
            migrate(
                migrator.add_index(
                    table,
                    indexed_columns,
                    unique))
            flash('Index created successfully.', 'success')
            return redirect(url_for('table_structure', table=table))
        else:
            flash('One or more columns must be selected.', 'danger')

    return render_template(
        'add_index.html',
        columns=columns,
        indexed_columns=indexed_columns,
        table=table,
        unique=unique)

@app.route('/<table>/drop-index/', methods=['GET', 'POST'])
@require_table
def drop_index(table):
    request_data = get_request_data()
    name = request_data.get('name', '')
    indexes = get_indexes(table)
    index_names = [index.name for index in indexes]

    if request.method == 'POST':
        if name in index_names:
            migrate(migrator.drop_index(table, name))
            flash('Index "%s" was dropped successfully!' % name, 'success')
            return redirect(url_for('table_structure', table=table))
        else:
            flash('Index name is required.', 'danger')

    return render_template(
        'drop_index.html',
        indexes=indexes,
        index_names=index_names,
        name=name,
        table=table)

@app.route('/<table>/drop-trigger/', methods=['GET', 'POST'])
@require_table
def drop_trigger(table):
    request_data = get_request_data()
    name = request_data.get('name', '')
    triggers = get_triggers(table)
    trigger_names = [trigger.name for trigger in triggers]

    if request.method == 'POST':
        if name in trigger_names:
            dataset.query('DROP TRIGGER "%s";' % name)
            flash('Trigger "%s" was dropped successfully!' % name, 'success')
            return redirect(url_for('table_structure', table=table))
        else:
            flash('Trigger name is required.', 'danger')

    return render_template(
        'drop_trigger.html',
        triggers=triggers,
        trigger_names=trigger_names,
        name=name,
        table=table)

@app.route('/<table>/content/')
@require_table
def table_content(table):
    page_number = int(request.args.get('page') or 1)
    if page_number > 1:
        previous_page = page_number - 1
    else:
        previous_page = None

    ds_table = dataset[table]
    total_rows = ds_table.all().count()
    rows_per_page = app.config['ROWS_PER_PAGE']
    total_pages = math.ceil(total_rows / float(rows_per_page))
    if page_number < total_pages:
        next_page = page_number + 1
    else:
        next_page = None

    query = ds_table.all().paginate(page_number, rows_per_page)

    ordering = request.args.get('ordering')
    if ordering:
        field = ds_table.model_class._meta.columns[ordering.lstrip('-')]
        if ordering.startswith('-'):
            field = field.desc()
        query = query.order_by(field)

    field_names = ds_table.columns
    columns = [field.db_column
               for field in ds_table.model_class._meta.get_fields()]

    return render_template(
        'table_content.html',
        columns=columns,
        ds_table=ds_table,
        field_names=field_names,
        next_page=next_page,
        ordering=ordering,
        page=page_number,
        previous_page=previous_page,
        query=query,
        table=table,
        total_rows=total_rows)

@app.route('/<table>/query/', methods=['GET', 'POST'])
@require_table
def table_query(table):
    data = []
    data_description = error = row_count = sql = None

    if request.method == 'POST':
        sql = request.form['sql']
        if 'export_json' in request.form:
            return export(table, sql, 'json')
        elif 'export_csv' in request.form:
            return export(table, sql, 'csv')

        try:
            cursor = dataset.query(sql)
        except Exception as exc:
            error = str(exc)
        else:
            data = cursor.fetchall()[:app.config['MAX_RESULT_SIZE']]
            data_description = cursor.description
            row_count = cursor.rowcount
    else:
        sql = 'SELECT *\nFROM "%s"' % (table)

    return render_template(
        'table_query.html',
        data=data,
        data_description=data_description,
        error=error,
        query_images=get_query_images(),
        row_count=row_count,
        sql=sql,
        table=table)

def export(table, sql, export_format):
    model_class = dataset[table].model_class
    query = model_class.raw(sql).dicts()
    buf = StringIO()
    if export_format == 'json':
        kwargs = {'indent': 2}
        filename = '%s-export.json' % table
        mimetype = 'text/javascript'
    else:
        kwargs = {}
        filename = '%s-export.csv' % table
        mimetype = 'text/csv'

    dataset.freeze(query, export_format, file_obj=buf, **kwargs)

    response_data= buf.getvalue()
    response = make_response(response_data)
    response.headers['Content-Length'] = len(response_data)
    response.headers['Content-Type'] = mimetype
    response.headers['Content-Disposition'] = 'attachment; filename=%s' % (
        filename)
    response.headers['Expires'] = 0
    response.headers['Pragma'] = 'public'
    return response

@app.route('/<table>/import/', methods=['GET', 'POST'])
@require_table
def table_import(table):
    count = None
    request_data = get_request_data()
    strict = bool(request_data.get('strict'))

    if request.method == 'POST':
        file_obj = request.files.get('file')
        if not file_obj:
            flash('Please select an import file.', 'danger')
        elif not file_obj.filename.lower().endswith(('.csv', '.json')):
            flash('Unsupported file-type. Must be a .json or .csv file.',
                  'danger')
        else:
            if file_obj.filename.lower().endswith('.json'):
                format = 'json'
            else:
                format = 'csv'
            try:
                with dataset.transaction():
                    count = dataset.thaw(
                        table,
                        format=format,
                        file_obj=file_obj.stream,
                        strict=strict)
            except Exception as exc:
                flash('Error importing file: %s' % exc, 'danger')
            else:
                flash(
                    'Successfully imported %s objects from %s.' % (
                        count, file_obj.filename),
                    'success')
                return redirect(url_for('table_content', table=table))

    return render_template(
        'table_import.html',
        count=count,
        strict=strict,
        table=table)

@app.route('/<table>/drop/', methods=['GET', 'POST'])
@require_table
def drop_table(table):
    if request.method == 'POST':
        model_class = dataset[table].model_class
        model_class.drop_table()
        flash('Table "%s" dropped successfully.' % table, 'success')
        return redirect(url_for('index'))

    return render_template('drop_table.html', table=table)

@app.template_filter('value_filter')
def value_filter(value, max_length=50):
    try:
        value = escape(value)
    except UnicodeDecodeError as e:
        charset = chardet.detect(value)
        encoding = charset.get('encoding')
        if encoding:
            value = unicode(value, encoding)
        else:
            value = repr(value)
        value = escape(value)
    if isinstance(value, basestring):
        if len(value) > max_length:
            return ('<span class="truncated">%s</span> '
                    '<span class="full" style="display:none;">%s</span>'
                    '<a class="toggle-value" href="#">...</a>') % (
                        value[:max_length],
                        value)
    return value

def get_query_images():
    accum = []
    image_dir = os.path.join(app.static_folder, 'img')
    if not os.path.exists(image_dir):
        return accum
    for filename in sorted(os.listdir(image_dir)):
        basename = os.path.splitext(os.path.basename(filename))[0]
        parts = basename.split('-')
        accum.append((parts, os.path.join('img', filename)))
    return accum

#
# Flask application helpers.
#

@app.context_processor
def _general():
    database_filename = os.path.realpath(dataset._database.database)
    stat = os.stat(database_filename)
    ts_to_dt = datetime.datetime.fromtimestamp
    indexes = dataset.query(
        'SELECT name, tbl_name FROM sqlite_master '
        'WHERE type=? ORDER BY tbl_name, name;',
        ('index',)).fetchall()
    triggers = dataset.query(
        'SELECT name, tbl_name, sql FROM sqlite_master '
        'WHERE type=? ORDER BY tbl_name, name',
        ('trigger',)).fetchall()
    return {
        'database': os.path.basename(database_filename),
        'database_filename': database_filename,
        'database_size': stat.st_size,
        'database_created': ts_to_dt(stat.st_ctime),
        'database_modified': ts_to_dt(stat.st_mtime),
        'indexes': indexes,
        'tables': dataset.tables,
        'triggers': triggers,
        'virtual_tables': get_virtual_tables(),
        'virtual_tables_corollary': get_corollary_virtual_tables(),
    }

@app.context_processor
def _now():
    return {'now': datetime.datetime.now()}

@app.before_request
def _connect_db():
    dataset.connect()

@app.teardown_request
def _close_db(exc):
    if not dataset._database.is_closed():
        dataset.close()

#
# Script options.
#

def get_option_parser():
    parser = optparse.OptionParser()
    parser.add_option(
        '-p',
        '--port',
        default=8080,
        help='Port for web interface, default=8080',
        type='int')
    parser.add_option(
        '-H',
        '--host',
        default='127.0.0.1',
        help='Host for web interface, default=127.0.0.1')
    parser.add_option(
        '-d',
        '--debug',
        action='store_true',
        help='Run server in debug mode')
    parser.add_option(
        '-x',
        '--no-browser',
        action='store_false',
        default=True,
        dest='browser',
        help='Do not automatically open browser page.')
    return parser

def die(msg, exit_code=1):
    sys.stderr.write('%s\n' % msg)
    sys.stderr.flush()
    sys.exit(exit_code)

def open_browser_tab(host, port):
    url = 'http://%s:%s/' % (host, port)

    def _open_tab(url):
        time.sleep(1.5)
        webbrowser.open_new_tab(url)

    thread = threading.Thread(target=_open_tab, args=(url,))
    thread.daemon = True
    thread.start()

def main():
    # This function exists to act as a console script entry-point.
    parser = get_option_parser()
    options, args = parser.parse_args()
    if not args:
        die('Error: missing required path to database file.')

    db_file = args[0]
    global dataset
    global migrator
    dataset = DataSet('sqlite:///%s' % db_file)
    migrator = dataset._migrator
    if options.browser:
        open_browser_tab(options.host, options.port)
    app.run(host=options.host, port=options.port, debug=options.debug)


if __name__ == '__main__':
    main()
