# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function
import time
import os

import requests
import backoff

from requests.adapters import HTTPAdapter

from .contract import KongAdminContract, APIAdminContract, ConsumerAdminContract, PluginAdminContract, \
    APIPluginConfigurationAdminContract, BasicAuthAdminContract, KeyAuthAdminContract, OAuth2AdminContract
from .utils import add_url_params, assert_dict_keys_in, ensure_trailing_slash
from .compat import OK, CREATED, NO_CONTENT, NOT_FOUND, CONFLICT, urljoin
from .exceptions import ConflictError

KONG_MINIMUM_REQUEST_INTERVAL = float(os.getenv('KONG_MINIMUM_REQUEST_INTERVAL', 0))
KONG_REUSE_CONNECTIONS = int(os.getenv('KONG_REUSE_CONNECTIONS', '1')) == 1

def get_default_kong_headers():
    headers = {}
    if not KONG_REUSE_CONNECTIONS:
        headers.update({'Connection': 'close'})
    return headers


def raise_response_error(response, exception_class=None, is_json=True):
    exception_class = exception_class or ValueError
    assert issubclass(exception_class, BaseException)
    if is_json:
        raise exception_class(', '.join(['%s: %s' % (key, value) for (key, value) in response.json().items()]))
    raise exception_class(str(response))


class ThrottlingHTTPAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        super(ThrottlingHTTPAdapter, self).__init__(*args, **kwargs)
        self._last_request = None

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if self._last_request is not None and KONG_MINIMUM_REQUEST_INTERVAL > 0:
            diff = time.time() - self._last_request
            if 0 < diff < KONG_MINIMUM_REQUEST_INTERVAL:
                time.sleep(diff)
        result = super(ThrottlingHTTPAdapter, self).send(request, stream, timeout, verify, cert, proxies)
        self._last_request = time.time()
        return result


class RestClient(object):
    def __init__(self, api_url, headers=None):
        self.api_url = api_url
        self.headers = headers
        self._session = None

    @property
    def session(self):
        if self._session is None:
            self._session = requests.session()
            if KONG_MINIMUM_REQUEST_INTERVAL > 0:
                self._session.mount(self.api_url, ThrottlingHTTPAdapter())
        elif not KONG_REUSE_CONNECTIONS:
            self._session.close()
            self._session = None
            return self.session
        return self._session

    def get_headers(self, **headers):
        result = {}
        result.update(self.headers)
        result.update(headers)
        return result

    def get_url(self, *path, **query_params):
        url = ensure_trailing_slash(urljoin(self.api_url, '/'.join(path)))
        return add_url_params(url, query_params)


class APIPluginConfigurationAdminClient(APIPluginConfigurationAdminContract, RestClient):
    def __init__(self, api_admin, api_name_or_id, api_url):
        super(APIPluginConfigurationAdminClient, self).__init__(api_url, headers=get_default_kong_headers())

        self.api_admin = api_admin
        self.api_name_or_id = api_name_or_id

    def create(self, plugin_name, enabled=None, consumer_id=None, **fields):
        values = {}
        for key in fields:
            values['value.%s' % key] = fields[key]

        data = dict({
            'name': plugin_name,
            'consumer_id': consumer_id,
        }, **values)

        if enabled is not None and isinstance(enabled, bool):
            data['enabled'] = enabled

        response = self.session.post(self.get_url('apis', self.api_name_or_id, 'plugins'), data=data,
                                     headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code != CREATED:
            raise_response_error(response, ValueError)

        return result

    def create_or_update(self, plugin_name, plugin_configuration_id=None, enabled=None, consumer_id=None, **fields):
        values = {}
        for key in fields:
            values['value.%s' % key] = fields[key]

        data = dict({
            'name': plugin_name,
            'consumer_id': consumer_id,
        }, **values)

        if enabled is not None and isinstance(enabled, bool):
            data['enabled'] = enabled

        if plugin_configuration_id is not None:
            data['id'] = plugin_configuration_id

        response = self.session.put(self.get_url('apis', self.api_name_or_id, 'plugins'), data=data,
                                    headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code not in (CREATED, OK):
            raise_response_error(response, ValueError)

        return result

    def update(self, plugin_name, enabled=None, consumer_id=None, **fields):
        values = {}
        for key in fields:
            values['value.%s' % key] = fields[key]

        data_struct_update = dict({
            'name': plugin_name,
        }, **values)

        if consumer_id is not None:
            data_struct_update['consumer_id'] = consumer_id

        if enabled is not None and isinstance(enabled, bool):
            data_struct_update['enabled'] = enabled

        url = self.get_url('apis', self.api_name_or_id, 'plugins', plugin_name)

        response = self.session.patch(url, data=data_struct_update, headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    def list(self, size=100, offset=None, **filter_fields):
        assert_dict_keys_in(filter_fields, ['id', 'name', 'api_id', 'consumer_id'])

        query_params = filter_fields
        query_params['size'] = size

        if offset is not None:
            query_params['offset'] = offset

        url = self.get_url('apis', self.api_name_or_id, 'plugins', **query_params)
        response = self.session.get(url, headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    @backoff.on_exception(backoff.expo, ValueError, max_tries=3)
    def delete(self, plugin_name_or_id):
        response = self.session.delete(self.get_url('apis', self.api_name_or_id, 'plugins', plugin_name_or_id),
                                       headers=self.get_headers())

        if response.status_code not in (NO_CONTENT, NOT_FOUND):
            raise ValueError('Could not delete Plugin Configuration (status: %s): %s' % (
                response.status_code, plugin_name_or_id))

    def count(self):
        response = self.session.get(self.get_url('apis', self.api_name_or_id, 'plugins'), headers=self.get_headers())
        result = response.json()
        amount = result.get('total', len(result.get('data')))
        return amount


class APIAdminClient(APIAdminContract, RestClient):
    def __init__(self, api_url):
        super(APIAdminClient, self).__init__(api_url, headers=get_default_kong_headers())

    def count(self):
        response = self.session.get(self.get_url('apis'), headers=self.get_headers())
        result = response.json()
        amount = result.get('total', len(result.get('data')))
        return amount

    def add(self, target_url, name=None, public_dns=None, path=None, strip_path=False, preserve_host=False):
        response = self.session.post(self.get_url('apis'), data={
            'name': name,
            'public_dns': public_dns or None,  # Empty strings are not allowed
            'path': path or None,  # Empty strings are not allowed
            'strip_path': strip_path,
            'preserve_host': preserve_host,
            'target_url': target_url
        }, headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code != CREATED:
            raise_response_error(response, ValueError)

        return result

    def add_or_update(self, target_url, api_id=None, name=None, public_dns=None, path=None, strip_path=False,
                      preserve_host=False):
        data = {
            'name': name,
            'public_dns': public_dns or None,  # Empty strings are not allowed
            'path': path or None,  # Empty strings are not allowed
            'strip_path': strip_path,
            'preserve_host': preserve_host,
            'target_url': target_url
        }

        if api_id is not None:
            data['id'] = api_id

        response = self.session.put(self.get_url('apis'), data=data, headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code not in (CREATED, OK):
            raise_response_error(response, ValueError)

        return result

    def update(self, name_or_id, target_url, **fields):
        assert_dict_keys_in(fields, ['name', 'public_dns', 'path', 'strip_path', 'preserve_host'])
        response = self.session.patch(self.get_url('apis', name_or_id), data=dict({
            'target_url': target_url
        }, **fields), headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    @backoff.on_exception(backoff.expo, ValueError, max_tries=3)
    def delete(self, name_or_id):
        response = self.session.delete(self.get_url('apis', name_or_id), headers=self.get_headers())

        if response.status_code not in (NO_CONTENT, NOT_FOUND):
            raise ValueError('Could not delete API (status: %s): %s' % (response.status_code, name_or_id))

    def retrieve(self, name_or_id):
        response = self.session.get(self.get_url('apis', name_or_id), headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    def list(self, size=100, offset=None, **filter_fields):
        assert_dict_keys_in(filter_fields, ['id', 'name', 'public_dns', 'path'])

        query_params = filter_fields
        query_params['size'] = size

        if offset:
            query_params['offset'] = offset

        url = self.get_url('apis', **query_params)
        response = self.session.get(url, headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    def plugins(self, name_or_id):
        return APIPluginConfigurationAdminClient(self, name_or_id, self.api_url)


class BasicAuthAdminClient(BasicAuthAdminContract, RestClient):
    def __init__(self, consumer_admin, consumer_id, api_url):
        super(BasicAuthAdminClient, self).__init__(api_url, headers=get_default_kong_headers())

        self.consumer_admin = consumer_admin
        self.consumer_id = consumer_id

    def create_or_update(self, basic_auth_id=None, username=None, password=None):
        data = {
            'username': username,
            'password': password,
        }

        if basic_auth_id is not None:
            data['id'] = basic_auth_id

        response = self.session.put(self.get_url('consumers', self.consumer_id, 'basicauth'), data=data,
                                    headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code not in (CREATED, OK):
            raise_response_error(response, ValueError)

        return result

    def create(self, username, password):
        response = self.session.post(self.get_url('consumers', self.consumer_id, 'basicauth'), data={
            'username': username,
            'password': password,
        }, headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code != CREATED:
            raise_response_error(response, ValueError)

        return result

    def list(self, size=100, offset=None, **filter_fields):
        assert_dict_keys_in(filter_fields, ['id', 'username'])

        query_params = filter_fields
        query_params['size'] = size

        if offset:
            query_params['offset'] = offset

        url = self.get_url('consumers', self.consumer_id, 'basicauth', **query_params)
        response = self.session.get(url, headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    @backoff.on_exception(backoff.expo, ValueError, max_tries=3)
    def delete(self, basic_auth_id):
        url = self.get_url('consumers', self.consumer_id, 'basicauth', basic_auth_id)
        response = self.session.delete(url, headers=self.get_headers())

        if response.status_code not in (NO_CONTENT, NOT_FOUND):
            raise ValueError('Could not delete Basic Auth (status: %s): %s for Consumer: %s' % (
                response.status_code, basic_auth_id, self.consumer_id))

    def retrieve(self, basic_auth_id):
        response = self.session.get(self.get_url('consumers', self.consumer_id, 'basicauth', basic_auth_id),
                                    headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    def count(self):
        response = self.session.get(self.get_url('consumers', self.consumer_id, 'basicauth'),
                                    headers=self.get_headers())
        result = response.json()
        amount = result.get('total', len(result.get('data')))
        return amount

    def update(self, basic_auth_id, **fields):
        assert_dict_keys_in(fields, ['username', 'password'])
        response = self.session.patch(
            self.get_url('consumers', self.consumer_id, 'basicauth', basic_auth_id), data=fields,
            headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result


class KeyAuthAdminClient(KeyAuthAdminContract, RestClient):
    def __init__(self, consumer_admin, consumer_id, api_url):
        super(KeyAuthAdminClient, self).__init__(api_url, headers=get_default_kong_headers())

        self.consumer_admin = consumer_admin
        self.consumer_id = consumer_id

    def create_or_update(self, key_auth_id=None, key=None):
        data = {
            'key': key
        }

        if key_auth_id is not None:
            data['id'] = key_auth_id

        response = self.session.put(self.get_url('consumers', self.consumer_id, 'keyauth'), data=data,
                                    headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code not in (CREATED, OK):
            raise_response_error(response, ValueError)

        return result

    def create(self, key=None):
        response = self.session.post(self.get_url('consumers', self.consumer_id, 'keyauth'), data={
            'key': key,
        }, headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code != CREATED:
            raise_response_error(response, ValueError)

        return result

    def list(self, size=100, offset=None, **filter_fields):
        assert_dict_keys_in(filter_fields, ['id', 'key'])

        query_params = filter_fields
        query_params['size'] = size

        if offset:
            query_params['offset'] = offset

        url = self.get_url('consumers', self.consumer_id, 'keyauth', **query_params)
        response = self.session.get(url, headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    @backoff.on_exception(backoff.expo, ValueError, max_tries=3)
    def delete(self, key_auth_id):
        url = self.get_url('consumers', self.consumer_id, 'keyauth', key_auth_id)
        response = self.session.delete(url, headers=self.get_headers())

        if response.status_code not in (NO_CONTENT, NOT_FOUND):
            raise ValueError('Could not delete Key Auth (status: %s): %s for Consumer: %s' % (
                response.status_code, key_auth_id, self.consumer_id))

    def retrieve(self, key_auth_id):
        response = self.session.get(self.get_url('consumers', self.consumer_id, 'keyauth', key_auth_id),
                                    headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    def count(self):
        response = self.session.get(self.get_url('consumers', self.consumer_id, 'keyauth'),
                                    headers=self.get_headers())
        result = response.json()
        amount = result.get('total', len(result.get('data')))
        return amount

    def update(self, key_auth_id, **fields):
        assert_dict_keys_in(fields, ['key'])
        response = self.session.patch(
            self.get_url('consumers', self.consumer_id, 'keyauth', key_auth_id), data=fields,
            headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result


class OAuth2AdminClient(OAuth2AdminContract, RestClient):
    def __init__(self, consumer_admin, consumer_id, api_url):
        super(OAuth2AdminClient, self).__init__(api_url, headers=get_default_kong_headers())

        self.consumer_admin = consumer_admin
        self.consumer_id = consumer_id

    def create_or_update(self, oauth2_id=None, name=None, redirect_uri=None, client_id=None, client_secret=None):
        data = {
            'name': name,
            'redirect_uri': redirect_uri,
            'client_id': client_id,
            'client_secret': client_secret
        }

        if oauth2_id is not None:
            data['id'] = oauth2_id

        response = self.session.put(self.get_url('consumers', self.consumer_id, 'oauth2'), data=data,
                                    headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code not in (CREATED, OK):
            raise_response_error(response, ValueError)

        return result

    def create(self, name, redirect_uri, client_id=None, client_secret=None):
        response = self.session.post(self.get_url('consumers', self.consumer_id, 'oauth2'), data={
            'name': name,
            'redirect_uri': redirect_uri,
            'client_id': client_id,
            'client_secret': client_secret
        }, headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code != CREATED:
            raise_response_error(response, ValueError)

        return result

    def list(self, size=100, offset=None, **filter_fields):
        assert_dict_keys_in(filter_fields, ['id', 'name', 'redirect_url', 'client_id'])

        query_params = filter_fields
        query_params['size'] = size

        if offset:
            query_params['offset'] = offset

        url = self.get_url('consumers', self.consumer_id, 'oauth2', **query_params)
        response = self.session.get(url, headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    @backoff.on_exception(backoff.expo, ValueError, max_tries=3)
    def delete(self, oauth2_id):
        url = self.get_url('consumers', self.consumer_id, 'oauth2', oauth2_id)
        response = self.session.delete(url, headers=self.get_headers())

        if response.status_code not in (NO_CONTENT, NOT_FOUND):
            raise ValueError('Could not delete OAuth2 (status: %s): %s for Consumer: %s' % (
                response.status_code, oauth2_id, self.consumer_id))

    def retrieve(self, oauth2_id):
        response = self.session.get(self.get_url('consumers', self.consumer_id, 'oauth2', oauth2_id),
                                    headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    def count(self):
        response = self.session.get(self.get_url('consumers', self.consumer_id, 'oauth2'),
                                    headers=self.get_headers())
        result = response.json()
        amount = result.get('total', len(result.get('data')))
        return amount

    def update(self, oauth2_id, **fields):
        assert_dict_keys_in(fields, ['name', 'redirect_uri', 'client_id', 'client_secret'])
        response = self.session.patch(
            self.get_url('consumers', self.consumer_id, 'oauth2', oauth2_id), data=fields,
            headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result


class ConsumerAdminClient(ConsumerAdminContract, RestClient):
    def __init__(self, api_url):
        super(ConsumerAdminClient, self).__init__(api_url, headers=get_default_kong_headers())

    def count(self):
        response = self.session.get(self.get_url('consumers'), headers=self.get_headers())
        result = response.json()
        amount = result.get('total', len(result.get('data')))
        return amount

    def create(self, username=None, custom_id=None):
        response = self.session.post(self.get_url('consumers'), data={
            'username': username,
            'custom_id': custom_id,
        }, headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code != CREATED:
            raise_response_error(response, ValueError)

        return result

    def create_or_update(self, consumer_id=None, username=None, custom_id=None):
        data = {
            'username': username,
            'custom_id': custom_id,
        }

        if consumer_id is not None:
            data['id'] = consumer_id

        response = self.session.put(self.get_url('consumers'), data=data, headers=self.get_headers())
        result = response.json()
        if response.status_code == CONFLICT:
            raise_response_error(response, ConflictError)
        elif response.status_code not in (CREATED, OK):
            raise_response_error(response, ValueError)

        return result

    def update(self, username_or_id, **fields):
        assert_dict_keys_in(fields, ['username', 'custom_id'])
        response = self.session.patch(self.get_url('consumers', username_or_id), data=fields,
                                      headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    def list(self, size=100, offset=None, **filter_fields):
        assert_dict_keys_in(filter_fields, ['id', 'custom_id', 'username'])

        query_params = filter_fields
        query_params['size'] = size

        if offset:
            query_params['offset'] = offset

        url = self.get_url('consumers', **query_params)
        response = self.session.get(url, headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    @backoff.on_exception(backoff.expo, ValueError, max_tries=3)
    def delete(self, username_or_id):
        response = self.session.delete(self.get_url('consumers', username_or_id), headers=self.get_headers())

        if response.status_code not in (NO_CONTENT, NOT_FOUND):
            raise ValueError('Could not delete Consumer (status: %s): %s' % (response.status_code, username_or_id))

    def retrieve(self, username_or_id):
        response = self.session.get(self.get_url('consumers', username_or_id), headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    def basic_auth(self, username_or_id):
        return BasicAuthAdminClient(self, username_or_id, self.api_url)

    def key_auth(self, username_or_id):
        return KeyAuthAdminClient(self, username_or_id, self.api_url)

    def oauth2(self, username_or_id):
        return OAuth2AdminClient(self, username_or_id, self.api_url)


class PluginAdminClient(PluginAdminContract, RestClient):
    def __init__(self, api_url):
        super(PluginAdminClient, self).__init__(api_url, headers=get_default_kong_headers())

    def list(self):
        response = self.session.get(self.get_url('plugins'), headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result

    def retrieve_schema(self, plugin_name):
        response = self.session.get(self.get_url('plugins', plugin_name, 'schema'), headers=self.get_headers())
        result = response.json()

        if response.status_code != OK:
            raise_response_error(response, ValueError)

        return result


class KongAdminClient(KongAdminContract):
    def __init__(self, api_url):
        super(KongAdminClient, self).__init__(
            apis=APIAdminClient(api_url),
            consumers=ConsumerAdminClient(api_url),
            plugins=PluginAdminClient(api_url))
