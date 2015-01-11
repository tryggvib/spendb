import logging

from flask import Blueprint
from flask.ext.login import current_user
from restpager import Pager

from openspending.core import db
from openspending.model import Dataset
from openspending.auth import require
from openspending.lib import solr_util as solr
from openspending.lib.jsonexport import jsonify
from openspending.lib.helpers import url_for, get_dataset
from openspending.lib.indices import clear_index_cache
from openspending.views.cache import etag_cache_keygen


log = logging.getLogger(__name__)
blueprint = Blueprint('datasets_api3', __name__)


@blueprint.route('/datasets')
def index():
    q = Dataset.all_by_account(current_user)
    # TODO: Facets for territories and languages
    # TODO: filters on facet dimensions
    pager = Pager(q)
    return jsonify(pager)


@blueprint.route('/datasets/<name>')
def view(name):
    dataset = get_dataset(name)
    etag_cache_keygen(dataset)
    return jsonify(dataset)


@blueprint.route('/datasets', methods=['POST', 'PUT'])
def create():
    return jsonify({})


@blueprint.route('/datasets/<name>', methods=['DELETE'])
def delete(name):
    dataset = get_dataset(name)
    require.dataset.update(dataset)

    dataset.fact_table.drop()
    db.session.delete(dataset)
    db.session.commit()
    clear_index_cache()
    solr.drop_index(dataset.name)
    return jsonify({'status': 'deleted'}, status=410)