import time
import gzip
import re
import json
import uuid
import base64
import codecs
import random
import logging
import datetime
import xmltodict
import collections
import dateutil.parser
import urllib.parse
import six
import botocore.config
from pytz import timezone
from urllib.parse import parse_qs
from botocore.compat import urlsplit
from botocore.client import ClientError
from botocore.credentials import Credentials
from localstack.utils.auth import HmacV1QueryAuth
from botocore.awsrequest import create_request_object
from requests.models import Response, Request
from six.moves.urllib import parse as urlparse
from localstack import config, constants
from localstack.config import HOSTNAME, HOSTNAME_EXTERNAL, LOCALHOST_IP
from localstack.utils.aws import aws_stack
from localstack.services.s3 import multipart_content
from localstack.utils.common import (
    short_uid, timestamp_millis, to_str, to_bytes, clone, md5, get_service_protocol, now_utc, is_base64
)
from localstack.utils.analytics import event_publisher
from localstack.utils.http_utils import uses_chunked_encoding
from localstack.utils.persistence import PersistingProxyListener
from localstack.constants import TEST_AWS_ACCESS_KEY_ID, TEST_AWS_SECRET_ACCESS_KEY
from localstack.utils.aws.aws_responses import requests_response, requests_error_response_xml_signature_calculation

CONTENT_SHA256_HEADER = 'x-amz-content-sha256'
STREAMING_HMAC_PAYLOAD = 'STREAMING-AWS4-HMAC-SHA256-PAYLOAD'

# backend port (configured in s3_starter.py on startup)
PORT_S3_BACKEND = None

# mappings for S3 bucket notifications
S3_NOTIFICATIONS = {}

# mappings for bucket CORS settings
BUCKET_CORS = {}

# maps bucket name to lifecycle settings
BUCKET_LIFECYCLE = {}

# maps bucket name to replication settings
BUCKET_REPLICATIONS = {}

# maps bucket name to encryption settings
BUCKET_ENCRYPTIONS = {}

# maps bucket name to object lock settings
OBJECT_LOCK_CONFIGS = {}

# map to store the s3 expiry dates
OBJECT_EXPIRY = {}

# set up logger
LOGGER = logging.getLogger(__name__)

# XML namespace constants
XMLNS_S3 = 'http://s3.amazonaws.com/doc/2006-03-01/'

# see https://stackoverflow.com/questions/50480924/regex-for-s3-bucket-name#50484916
BUCKET_NAME_REGEX = (r'(?=^.{3,63}$)(?!^(\d+\.)+\d+$)' +
    r'(^(([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])\.)*([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])$)')

# list of destination types for bucket notifications
NOTIFICATION_DESTINATION_TYPES = ('Queue', 'Topic', 'CloudFunction', 'LambdaFunction')

# prefix for object metadata keys in headers and query params
OBJECT_METADATA_KEY_PREFIX = 'x-amz-meta-'

# response header overrides the client may request
ALLOWED_HEADER_OVERRIDES = {
    'response-content-type': 'Content-Type',
    'response-content-language': 'Content-Language',
    'response-expires': 'Expires',
    'response-cache-control': 'Cache-Control',
    'response-content-disposition': 'Content-Disposition',
    'response-content-encoding': 'Content-Encoding',
}

# From botocore's auth.py:
# https://github.com/boto/botocore/blob/30206ab9e9081c80fa68e8b2cb56296b09be6337/botocore/auth.py#L47
POLICY_EXPIRATION_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

# ignored_headers_lower conatins headers which don't get involved in signature calculations process
# these headers are being sent by the localstack by default.
IGNORED_HEADERS_LOWER = [
    'remote-addr', 'host', 'user-agent', 'accept-encoding',
    'accept', 'connection', 'origin',
    'x-forwarded-for', 'x-localstack-edge', 'authorization'
]

# params are required in presigned url
PRESIGN_QUERY_PARAMS = ['Signature', 'Expires', 'AWSAccessKeyId']


def event_type_matches(events, action, api_method):
    """ check whether any of the event types in `events` matches the
        given `action` and `api_method`, and return the first match. """
    events = events or []
    for event in events:
        regex = event.replace('*', '[^:]*')
        action_string = 's3:%s:%s' % (action, api_method)
        match = re.match(regex, action_string)
        if match:
            return match
    return False


def filter_rules_match(filters, object_path):
    """ check whether the given object path matches all of the given filters """
    filters = filters or {}
    s3_filter = _get_s3_filter(filters)
    for rule in s3_filter.get('FilterRule', []):
        rule_name_lower = rule['Name'].lower()
        if rule_name_lower == 'prefix':
            if not prefix_with_slash(object_path).startswith(prefix_with_slash(rule['Value'])):
                return False
        elif rule_name_lower == 'suffix':
            if not object_path.endswith(rule['Value']):
                return False
        else:
            LOGGER.warning('Unknown filter name: "%s"' % rule['Name'])
    return True


def _get_s3_filter(filters):
    return filters.get('S3Key', filters.get('Key', {}))


def prefix_with_slash(s):
    return s if s[0] == '/' else '/%s' % s


def get_event_message(event_name, bucket_name, file_name='testfile.txt', etag='', version_id=None, file_size=0):
    # Based on: http://docs.aws.amazon.com/AmazonS3/latest/dev/notification-content-structure.html
    bucket_name = normalize_bucket_name(bucket_name)
    return {
        'Records': [{
            'eventVersion': '2.0',
            'eventSource': 'aws:s3',
            'awsRegion': aws_stack.get_region(),
            'eventTime': timestamp_millis(),
            'eventName': event_name,
            'userIdentity': {
                'principalId': 'AIDAJDPLRKLG7UEXAMPLE'
            },
            'requestParameters': {
                'sourceIPAddress': '127.0.0.1'  # TODO determine real source IP
            },
            'responseElements': {
                'x-amz-request-id': short_uid(),
                'x-amz-id-2': 'eftixk72aD6Ap51TnqcoF8eFidJG9Z/2'  # Amazon S3 host that processed the request
            },
            's3': {
                's3SchemaVersion': '1.0',
                'configurationId': 'testConfigRule',
                'bucket': {
                    'name': bucket_name,
                    'ownerIdentity': {
                        'principalId': 'A3NL1KOZZKExample'
                    },
                    'arn': 'arn:aws:s3:::%s' % bucket_name
                },
                'object': {
                    'key': urllib.parse.quote(file_name),
                    'size': file_size,
                    'eTag': etag,
                    'versionId': version_id,
                    'sequencer': '0055AED6DCD90281E5'
                }
            }
        }]
    }


def send_notifications(method, bucket_name, object_path, version_id):
    for bucket, notifs in S3_NOTIFICATIONS.items():
        if normalize_bucket_name(bucket) == normalize_bucket_name(bucket_name):
            action = {'PUT': 'ObjectCreated', 'POST': 'ObjectCreated', 'DELETE': 'ObjectRemoved'}[method]
            # TODO: support more detailed methods, e.g., DeleteMarkerCreated
            # http://docs.aws.amazon.com/AmazonS3/latest/dev/NotificationHowTo.html
            if action == 'ObjectCreated' and method == 'POST':
                api_method = 'CompleteMultipartUpload'
            else:
                api_method = {'PUT': 'Put', 'POST': 'Post', 'DELETE': 'Delete'}[method]

            event_name = '%s:%s' % (action, api_method)
            for notif in notifs:
                send_notification_for_subscriber(notif, bucket_name, object_path,
                    version_id, api_method, action, event_name)


def send_notification_for_subscriber(notif, bucket_name, object_path, version_id, api_method, action, event_name):
    bucket_name = normalize_bucket_name(bucket_name)

    if not event_type_matches(notif['Event'], action, api_method) or \
            not filter_rules_match(notif.get('Filter'), object_path):
        return

    key = urlparse.unquote(object_path.replace('//', '/'))[1:]

    s3_client = aws_stack.connect_to_service('s3')
    object_data = {}
    try:
        object_data = s3_client.head_object(Bucket=bucket_name, Key=key)

    except botocore.exceptions.ClientError:
        pass

    # build event message
    message = get_event_message(
        event_name=event_name,
        bucket_name=bucket_name,
        file_name=key,
        etag=object_data.get('ETag', ''),
        file_size=object_data.get('ContentLength', 0),
        version_id=version_id
    )
    message = json.dumps(message)

    if notif.get('Queue'):
        sqs_client = aws_stack.connect_to_service('sqs')
        try:
            queue_url = aws_stack.sqs_queue_url_for_arn(notif['Queue'])
            sqs_client.send_message(QueueUrl=queue_url, MessageBody=message)
        except Exception as e:
            LOGGER.warning('Unable to send notification for S3 bucket "%s" to SQS queue "%s": %s' %
                (bucket_name, notif['Queue'], e))
    if notif.get('Topic'):
        sns_client = aws_stack.connect_to_service('sns')
        try:
            sns_client.publish(TopicArn=notif['Topic'], Message=message, Subject='Amazon S3 Notification')
        except Exception:
            LOGGER.warning('Unable to send notification for S3 bucket "%s" to SNS topic "%s".' %
                (bucket_name, notif['Topic']))
    # CloudFunction and LambdaFunction are semantically identical
    lambda_function_config = notif.get('CloudFunction') or notif.get('LambdaFunction')
    if lambda_function_config:
        # make sure we don't run into a socket timeout
        connection_config = botocore.config.Config(read_timeout=300)
        lambda_client = aws_stack.connect_to_service('lambda', config=connection_config)
        try:
            lambda_client.invoke(FunctionName=lambda_function_config,
                                 InvocationType='Event', Payload=message)
        except Exception:
            LOGGER.warning('Unable to send notification for S3 bucket "%s" to Lambda function "%s".' %
                (bucket_name, lambda_function_config))

    if not filter(lambda x: notif.get(x), NOTIFICATION_DESTINATION_TYPES):
        LOGGER.warning('Neither of %s defined for S3 notification.' %
            '/'.join(NOTIFICATION_DESTINATION_TYPES))


def get_cors(bucket_name):
    bucket_name = normalize_bucket_name(bucket_name)
    response = Response()

    exists, code = bucket_exists(bucket_name)
    if not exists:
        response.status_code = code
        return response

    cors = BUCKET_CORS.get(bucket_name)
    if not cors:
        cors = {
            'CORSConfiguration': []
        }
    body = xmltodict.unparse(cors)
    response._content = body
    response.status_code = 200
    return response


def set_cors(bucket_name, cors):
    bucket_name = normalize_bucket_name(bucket_name)
    response = Response()

    exists, code = bucket_exists(bucket_name)
    if not exists:
        response.status_code = code
        return response

    if not isinstance(cors, dict):
        cors = xmltodict.parse(cors)

    BUCKET_CORS[bucket_name] = cors
    response.status_code = 200
    return response


def delete_cors(bucket_name):
    bucket_name = normalize_bucket_name(bucket_name)
    response = Response()

    exists, code = bucket_exists(bucket_name)
    if not exists:
        response.status_code = code
        return response

    BUCKET_CORS.pop(bucket_name, {})
    response.status_code = 200
    return response


def convert_origins_into_list(allowed_origins):
    if isinstance(allowed_origins, list):
        return allowed_origins
    return [allowed_origins]


def append_cors_headers(bucket_name, request_method, request_headers, response):
    bucket_name = normalize_bucket_name(bucket_name)

    cors = BUCKET_CORS.get(bucket_name)
    if not cors:
        return

    origin = request_headers.get('Origin', '')
    rules = cors['CORSConfiguration']['CORSRule']
    if not isinstance(rules, list):
        rules = [rules]
    for rule in rules:
        # add allow-origin header
        allowed_methods = rule.get('AllowedMethod', [])
        if request_method in allowed_methods:
            allowed_origins = rule.get('AllowedOrigin', [])
            # when only one origin is being set in cors then the allowed_origins is being
            # reflected as a string here,so making it a list and then proceeding.
            allowed_origins = convert_origins_into_list(allowed_origins)
            for allowed in allowed_origins:
                if origin in allowed or re.match(allowed.replace('*', '.*'), origin):
                    response.headers['Access-Control-Allow-Origin'] = origin
                    if 'ExposeHeader' in rule:
                        expose_headers = rule['ExposeHeader']
                        response.headers['Access-Control-Expose-Headers'] = \
                            ','.join(expose_headers) if isinstance(expose_headers, list) else expose_headers
                    break


def append_aws_request_troubleshooting_headers(response):
    gen_amz_request_id = ''.join(random.choice('0123456789ABCDEF') for i in range(16))
    if response.headers.get('x-amz-request-id') is None:
        response.headers['x-amz-request-id'] = gen_amz_request_id
    if response.headers.get('x-amz-id-2') is None:
        response.headers['x-amz-id-2'] = 'MzRISOwyjmnup' + gen_amz_request_id + '7/JypPGXLh0OVFGcJaaO3KW/hRAqKOpIEEp'


def add_accept_range_header(response):
    if response.headers.get('accept-ranges') is None:
        response.headers['accept-ranges'] = 'bytes'


def is_object_expired(path):
    object_expiry = get_object_expiry(path)
    if not object_expiry:
        return False
    if dateutil.parser.parse(object_expiry) > \
            datetime.datetime.now(timezone(dateutil.parser.parse(object_expiry).tzname())):
        return False
    return True


def set_object_expiry(path, headers):
    OBJECT_EXPIRY[path] = headers.get('expires')


def get_object_expiry(path):
    return OBJECT_EXPIRY.get(path)


def is_url_already_expired(expiry_timestamp):
    if int(expiry_timestamp) < int(now_utc()):
        return True
    return False


def add_reponse_metadata_headers(response):
    if response.headers.get('content-language') is None:
        response.headers['content-language'] = 'en-US'
    if response.headers.get('cache-control') is None:
        response.headers['cache-control'] = 'no-cache'
    if response.headers.get('content-encoding') is None:
        if not uses_chunked_encoding(response):
            response.headers['content-encoding'] = 'identity'


def append_last_modified_headers(response, content=None):
    """Add Last-Modified header with current time
    (if the response content is an XML containing <LastModified>, add that instead)"""

    time_format = '%a, %d %b %Y %H:%M:%S GMT'  # TimeFormat
    try:
        if content:
            last_modified_str = re.findall(r'<LastModified>([^<]*)</LastModified>', content)
            if last_modified_str:
                last_modified_str = last_modified_str[0]
                last_modified_time_format = dateutil.parser.parse(last_modified_str).strftime(time_format)
                response.headers['Last-Modified'] = last_modified_time_format
    except TypeError as err:
        LOGGER.debug('No parsable content: %s' % err)
    except ValueError as err:
        LOGGER.error('Failed to parse LastModified: %s' % err)
    except Exception as err:
        LOGGER.error('Caught generic exception (parsing LastModified): %s' % err)
    # if cannot parse any LastModified, just continue

    try:
        if response.headers.get('Last-Modified', '') == '':
            response.headers['Last-Modified'] = datetime.datetime.now().strftime(time_format)
    except Exception as err:
        LOGGER.error('Caught generic exception (setting LastModified header): %s' % err)


def append_list_objects_marker(method, path, data, response):
    if 'marker=' in path:
        content = to_str(response.content)
        if '<ListBucketResult' in content and '<Marker>' not in content:
            parsed = urlparse.urlparse(path)
            query_map = urlparse.parse_qs(parsed.query)
            insert = '<Marker>%s</Marker>' % query_map.get('marker')[0]
            response._content = content.replace('</ListBucketResult>', '%s</ListBucketResult>' % insert)
            response.headers.pop('Content-Length', None)


def append_metadata_headers(method, query_map, headers):
    for key, value in query_map.items():
        if key.lower().startswith(OBJECT_METADATA_KEY_PREFIX):
            if headers.get(key) is None:
                headers[key] = value[0]


def fix_location_constraint(response):
    """ Make sure we return a valid non-empty LocationConstraint, as this otherwise breaks Serverless. """
    try:
        content = to_str(response.content or '') or ''
    except Exception:
        content = ''
    if 'LocationConstraint' in content:
        pattern = r'<LocationConstraint([^>]*)>\s*</LocationConstraint>'
        replace = r'<LocationConstraint\1>%s</LocationConstraint>' % aws_stack.get_region()
        response._content = re.sub(pattern, replace, content)
        remove_xml_preamble(response)


def fix_range_content_type(bucket_name, path, headers, response):
    # Fix content type for Range requests - https://github.com/localstack/localstack/issues/1259
    if 'Range' not in headers:
        return

    s3_client = aws_stack.connect_to_service('s3')
    path = urlparse.unquote(path)
    key_name = get_key_name(path, headers)
    result = s3_client.head_object(Bucket=bucket_name, Key=key_name)
    content_type = result['ContentType']
    if response.headers.get('Content-Type') == 'text/html; charset=utf-8':
        response.headers['Content-Type'] = content_type


def fix_delete_objects_response(bucket_name, method, parsed_path, data, headers, response):
    # Deleting non-existing keys should not result in errors.
    # Fixes https://github.com/localstack/localstack/issues/1893
    if not (method == 'POST' and parsed_path.query == 'delete' and '<Delete' in to_str(data or '')):
        return
    content = to_str(response._content)
    if '<Error>' not in content:
        return
    result = xmltodict.parse(content).get('DeleteResult')
    errors = result.get('Error')
    errors = errors if isinstance(errors, list) else [errors]
    deleted = result.get('Deleted')
    if not isinstance(result.get('Deleted'), list):
        deleted = result['Deleted'] = [deleted] if deleted else []
    for entry in list(errors):
        if set(entry.keys()) == set(['Key']):
            errors.remove(entry)
            deleted.append(entry)
    if not errors:
        result.pop('Error')
    response._content = xmltodict.unparse({'DeleteResult': result})


def fix_metadata_key_underscores(request_headers={}, response=None):
    # fix for https://github.com/localstack/localstack/issues/1790
    underscore_replacement = '---'
    meta_header_prefix = 'x-amz-meta-'
    prefix_len = len(meta_header_prefix)
    updated = False
    for key in list(request_headers.keys()):
        if key.lower().startswith(meta_header_prefix):
            key_new = meta_header_prefix + key[prefix_len:].replace('_', underscore_replacement)
            if key != key_new:
                request_headers[key_new] = request_headers.pop(key)
                updated = True
    if response:
        for key in list(response.headers.keys()):
            if key.lower().startswith(meta_header_prefix):
                key_new = meta_header_prefix + key[prefix_len:].replace(underscore_replacement, '_')
                if key != key_new:
                    response.headers[key_new] = response.headers.pop(key)
    return updated


def fix_creation_date(method, path, response):
    if method != 'GET' or path != '/':
        return
    response._content = re.sub(r'(\.[0-9]+)(\+00:00)?</CreationDate>',
        r'\1Z</CreationDate>', to_str(response._content))


def fix_delimiter(data, headers, response):
    if response.status_code == 200 and response._content:
        c, xml_prefix, delimiter = response._content, '<?xml', '<Delimiter><'
        pattern = '[<]Delimiter[>]None[<]'
        if isinstance(c, bytes):
            xml_prefix, delimiter = xml_prefix.encode(), delimiter.encode()
            pattern = pattern.encode()
        if c.startswith(xml_prefix):
            response._content = re.compile(pattern).sub(delimiter, c)


def convert_to_chunked_encoding(method, path, response):
    if method != 'GET' or path != '/':
        return
    if response.headers.get('Transfer-Encoding', '').lower() == 'chunked':
        return
    response.headers['Transfer-Encoding'] = 'chunked'
    response.headers.pop('Content-Encoding', None)
    response.headers.pop('Content-Length', None)


def unquote(s):
    if (s[0], s[-1]) in (('"', '"'), ("'", "'")):
        return s[1:-1]
    return s


def ret304_on_etag(data, headers, response):
    etag = response.headers.get('ETag')
    if etag:
        match = headers.get('If-None-Match')
        if match and unquote(match) == unquote(etag):
            response.status_code = 304
            response._content = ''


def fix_etag_for_multipart(data, headers, response):
    # Fix for https://github.com/localstack/localstack/issues/1978
    if headers.get(CONTENT_SHA256_HEADER) == STREAMING_HMAC_PAYLOAD:
        try:
            if b'chunk-signature=' not in to_bytes(data):
                return
            correct_hash = md5(strip_chunk_signatures(data))
            tags = r'<ETag>%s</ETag>'
            pattern = r'(&#34;)?([^<&]+)(&#34;)?'
            replacement = r'\g<1>%s\g<3>' % correct_hash
            response._content = re.sub(tags % pattern, tags % replacement, to_str(response.content))
            if response.headers.get('ETag'):
                response.headers['ETag'] = re.sub(pattern, replacement, response.headers['ETag'])
        except Exception:
            pass


def remove_xml_preamble(response):
    """ Removes <?xml ... ?> from a response content """
    response._content = re.sub(r'^<\?[^\?]+\?>', '', to_str(response._content))


# --------------
# HELPER METHODS
#   for lifecycle/replication/encryption/...
# --------------

def get_lifecycle(bucket_name):
    bucket_name = normalize_bucket_name(bucket_name)
    exists, code, body = is_bucket_available(bucket_name)
    if not exists:
        return requests_response(body, status_code=code)

    lifecycle = BUCKET_LIFECYCLE.get(bucket_name)
    status_code = 200

    if not lifecycle:
        lifecycle = {
            'Error': {
                'Code': 'NoSuchLifecycleConfiguration',
                'Message': 'The lifecycle configuration does not exist'
            }
        }
        status_code = 404
    body = xmltodict.unparse(lifecycle)
    return requests_response(body, status_code=status_code)


def get_replication(bucket_name):
    bucket_name = normalize_bucket_name(bucket_name)
    exists, code, body = is_bucket_available(bucket_name)
    if not exists:
        return requests_response(body, status_code=code)

    replication = BUCKET_REPLICATIONS.get(bucket_name)
    status_code = 200
    if not replication:
        replication = {
            'Error': {
                'Code': 'ReplicationConfigurationNotFoundError',
                'Message': 'The replication configuration was not found'
            }
        }
        status_code = 404
    body = xmltodict.unparse(replication)
    return requests_response(body, status_code=status_code)


def get_encryption(bucket_name):
    bucket_name = normalize_bucket_name(bucket_name)
    exists, code, body = is_bucket_available(bucket_name)
    if not exists:
        return requests_response(body, status_code=code)

    encryption = BUCKET_ENCRYPTIONS.get(bucket_name)
    status_code = 200
    if not encryption:
        encryption = {
            'Error': {
                'Code': 'ServerSideEncryptionConfigurationNotFoundError',
                'Message': 'The server side encryption configuration was not found'
            }
        }
        status_code = 404
    body = xmltodict.unparse(encryption)
    return requests_response(body, status_code=status_code)


def get_object_lock(bucket_name):
    bucket_name = normalize_bucket_name(bucket_name)
    exists, code, body = is_bucket_available(bucket_name)
    if not exists:
        return requests_response(body, status_code=code)

    lock_config = OBJECT_LOCK_CONFIGS.get(bucket_name)
    status_code = 200
    if not lock_config:
        lock_config = {
            'Error': {
                'Code': 'ObjectLockConfigurationNotFoundError',
                'Message': 'Object Lock configuration does not exist for this bucket'
            }
        }
        status_code = 404
    body = xmltodict.unparse(lock_config)
    return requests_response(body, status_code=status_code)


def set_lifecycle(bucket_name, lifecycle):
    bucket_name = normalize_bucket_name(bucket_name)
    exists, code, body = is_bucket_available(bucket_name)
    if not exists:
        return requests_response(body, status_code=code)

    if isinstance(to_str(lifecycle), six.string_types):
        lifecycle = xmltodict.parse(lifecycle)
    BUCKET_LIFECYCLE[bucket_name] = lifecycle
    return 200


def set_replication(bucket_name, replication):
    bucket_name = normalize_bucket_name(bucket_name)
    exists, code, body = is_bucket_available(bucket_name)
    if not exists:
        return requests_response(body, status_code=code)

    if isinstance(to_str(replication), six.string_types):
        replication = xmltodict.parse(replication)
    BUCKET_REPLICATIONS[bucket_name] = replication
    return 200


def set_encryption(bucket_name, encryption):
    bucket_name = normalize_bucket_name(bucket_name)
    exists, code, body = is_bucket_available(bucket_name)
    if not exists:
        return requests_response(body, status_code=code)

    if isinstance(to_str(encryption), six.string_types):
        encryption = xmltodict.parse(encryption)
    BUCKET_ENCRYPTIONS[bucket_name] = encryption
    return 200


def set_object_lock(bucket_name, lock_config):
    bucket_name = normalize_bucket_name(bucket_name)
    exists, code, body = is_bucket_available(bucket_name)
    if not exists:
        return requests_response(body, status_code=code)

    if isinstance(to_str(lock_config), six.string_types):
        lock_config = xmltodict.parse(lock_config)
    OBJECT_LOCK_CONFIGS[bucket_name] = lock_config
    return 200


# -------------
# UTIL METHODS
# -------------

def strip_chunk_signatures(data):
    # For clients that use streaming v4 authentication, the request contains chunk signatures
    # in the HTTP body (see example below) which we need to strip as moto cannot handle them
    #
    # 17;chunk-signature=6e162122ec4962bea0b18bc624025e6ae4e9322bdc632762d909e87793ac5921
    # <payload data ...>
    # 0;chunk-signature=927ab45acd82fc90a3c210ca7314d59fedc77ce0c914d79095f8cc9563cf2c70
    data_new = ''
    if data is not None:
        data_new = re.sub(b'(^|\r\n)[0-9a-fA-F]+;chunk-signature=[0-9a-f]{64}(\r\n)(\r\n$)?', b'',
            to_bytes(data), flags=re.MULTILINE | re.DOTALL)

    return data_new


def is_bucket_available(bucket_name):
    body = {'Code': '200'}
    exists, code = bucket_exists(bucket_name)
    if not exists:
        body = {
            'Error': {
                'Code': code,
                'Message': 'The bucket does not exist'
            }
        }
        return exists, code, body

    return True, 200, body


def bucket_exists(bucket_name):
    """Tests for the existence of the specified bucket. Returns the error code
    if the bucket does not exist (200 if the bucket does exist).
    """
    bucket_name = normalize_bucket_name(bucket_name)

    s3_client = aws_stack.connect_to_service('s3')
    try:
        s3_client.head_bucket(Bucket=bucket_name)
    except ClientError as err:
        error_code = err.response.get('Error').get('Code')
        return False, error_code

    return True, 200


def check_content_md5(data, headers):
    actual = md5(strip_chunk_signatures(data))
    try:
        md5_header = headers['Content-MD5']
        if not is_base64(md5_header):
            raise Exception('Content-MD5 header is not in Base64 format: "%s"' % md5_header)
        expected = to_str(codecs.encode(base64.b64decode(md5_header), 'hex'))
    except Exception:
        return error_response('The Content-MD5 you specified is not valid.', 'InvalidDigest', status_code=400)
    if actual != expected:
        return error_response('The Content-MD5 you specified did not match what we received.',
            'BadDigest', status_code=400)


def error_response(message, code, status_code=400):
    result = {'Error': {'Code': code, 'Message': message}}
    content = xmltodict.unparse(result)
    headers = {'content-type': 'application/xml'}
    return requests_response(content, status_code=status_code, headers=headers)


def no_such_key_error(resource, requestId=None, status_code=400):
    result = {'Error': {'Code': 'NoSuchKey',
            'Message': 'The resource you requested does not exist',
            'Resource': resource, 'RequestId': requestId}}
    content = xmltodict.unparse(result)
    headers = {'content-type': 'application/xml'}
    return requests_response(content, status_code=status_code, headers=headers)


def token_expired_error(resource, requestId=None, status_code=400):
    result = {'Error': {'Code': 'ExpiredToken',
            'Message': 'The provided token has expired.',
            'Resource': resource, 'RequestId': requestId}}
    content = xmltodict.unparse(result)
    headers = {'content-type': 'application/xml'}
    return requests_response(content, status_code=status_code, headers=headers)


def expand_redirect_url(starting_url, key, bucket):
    """ Add key and bucket parameters to starting URL query string. """
    parsed = urlparse.urlparse(starting_url)
    query = collections.OrderedDict(urlparse.parse_qsl(parsed.query))
    query.update([('key', key), ('bucket', bucket)])

    redirect_url = urlparse.urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, urlparse.urlencode(query), None))

    return redirect_url


def is_bucket_specified_in_domain_name(path, headers):
    host = headers.get('host', '')
    return re.match(r'.*s3(\-website)?\.([^\.]+\.)?amazonaws.com', host)


def is_object_specific_request(path, headers):
    """ Return whether the given request is specific to a certain S3 object.
        Note: the bucket name is usually specified as a path parameter,
        but may also be part of the domain name! """
    bucket_in_domain = is_bucket_specified_in_domain_name(path, headers)
    parts = len(path.split('/'))
    return parts > (1 if bucket_in_domain else 2)


def normalize_bucket_name(bucket_name):
    bucket_name = bucket_name or ''
    # AWS appears to automatically convert upper to lower case chars in bucket names
    bucket_name = bucket_name.lower()
    return bucket_name


def get_key_name(path, headers):
    parsed = urlparse.urlparse(path)
    path_parts = parsed.path.lstrip('/').split('/', 1)

    if uses_path_addressing(headers):
        return path_parts[1]
    return path_parts[0]


def uses_path_addressing(headers):
    host = headers.get(constants.HEADER_LOCALSTACK_EDGE_URL, '').split('://')[-1] or headers['host']
    return host.startswith(HOSTNAME) or host.startswith(HOSTNAME_EXTERNAL) or host.startswith(LOCALHOST_IP)


def get_bucket_name(path, headers):
    parsed = urlparse.urlparse(path)

    # try pick the bucket_name from the path
    bucket_name = parsed.path.split('/')[1]

    # is the hostname not starting with a bucket name?
    if uses_path_addressing(headers):
        return normalize_bucket_name(bucket_name)

    # matches the common endpoints like
    #     - '<bucket_name>.s3.<region>.amazonaws.com'
    #     - '<bucket_name>.s3-<region>.amazonaws.com.cn'
    common_pattern = re.compile(r'^(.+)\.s3[.\-][a-z]{2}-[a-z]+-[0-9]{1,}'
                                r'\.amazonaws\.com(\.[a-z]+)?$')
    # matches dualstack endpoints like
    #     - <bucket_name>.s3.dualstack.<region>.amazonaws.com'
    #     - <bucket_name>.s3.dualstack.<region>.amazonaws.com.cn'
    dualstack_pattern = re.compile(r'^(.+)\.s3\.dualstack\.[a-z]{2}-[a-z]+-[0-9]{1,}'
                                   r'\.amazonaws\.com(\.[a-z]+)?$')
    # matches legacy endpoints like
    #     - '<bucket_name>.s3.amazonaws.com'
    #     - '<bucket_name>.s3-external-1.amazonaws.com.cn'
    legacy_patterns = re.compile(r'^(.+)\.s3\.?(-external-1)?\.amazonaws\.com(\.[a-z]+)?$')

    # if any of the above patterns match, the first captured group
    # will be returned as the bucket name
    host = headers['host']
    for pattern in [common_pattern, dualstack_pattern, legacy_patterns]:
        match = pattern.match(host)
        if match:
            bucket_name = match.groups()[0]
            break

    # we're either returning the original bucket_name,
    # or a pattern matched the host and we're returning that name instead
    return normalize_bucket_name(bucket_name)


def handle_notification_request(bucket, method, data):
    response = Response()
    response.status_code = 200
    response._content = ''
    if method == 'GET':
        # TODO check if bucket exists
        result = '<NotificationConfiguration xmlns="%s">' % XMLNS_S3
        if bucket in S3_NOTIFICATIONS:
            notifs = S3_NOTIFICATIONS[bucket]
            for notif in notifs:
                for dest in NOTIFICATION_DESTINATION_TYPES:
                    if dest in notif:
                        dest_dict = {
                            '%sConfiguration' % dest: {
                                'Id': uuid.uuid4(),
                                dest: notif[dest],
                                'Event': notif['Event'],
                                'Filter': notif['Filter']
                            }
                        }
                        result += xmltodict.unparse(dest_dict, full_document=False)
        result += '</NotificationConfiguration>'
        response._content = result

    if method == 'PUT':
        parsed = xmltodict.parse(data)
        notif_config = parsed.get('NotificationConfiguration')
        S3_NOTIFICATIONS[bucket] = []
        for dest in NOTIFICATION_DESTINATION_TYPES:
            config = notif_config.get('%sConfiguration' % (dest))
            configs = config if isinstance(config, list) else [config] if config else []
            for config in configs:
                events = config.get('Event')
                if isinstance(events, six.string_types):
                    events = [events]
                event_filter = config.get('Filter', {})
                # make sure FilterRule is an array
                s3_filter = _get_s3_filter(event_filter)
                if s3_filter and not isinstance(s3_filter.get('FilterRule', []), list):
                    s3_filter['FilterRule'] = [s3_filter['FilterRule']]
                # create final details dict
                notification_details = {
                    'Id': config.get('Id'),
                    'Event': events,
                    dest: config.get(dest),
                    'Filter': event_filter
                }
                S3_NOTIFICATIONS[bucket].append(clone(notification_details))
    return response


def remove_bucket_notification(bucket):
    S3_NOTIFICATIONS.pop(bucket, None)


def not_none_or(value, alternative):
    return value if value is not None else alternative


class ProxyListenerS3(PersistingProxyListener):
    def api_name(self):
        return 's3'

    @staticmethod
    def is_s3_copy_request(headers, path):
        return 'x-amz-copy-source' in headers or 'x-amz-copy-source' in path

    @staticmethod
    def get_201_response(key, bucket_name):
        return """
                <PostResponse>
                    <Location>{protocol}://{host}/{encoded_key}</Location>
                    <Bucket>{bucket}</Bucket>
                    <Key>{key}</Key>
                    <ETag>{etag}</ETag>
                </PostResponse>
                """.format(
            protocol=get_service_protocol(),
            host=config.HOSTNAME_EXTERNAL,
            encoded_key=urlparse.quote(key, safe=''),
            key=key,
            bucket=bucket_name,
            etag='d41d8cd98f00b204e9800998ecf8427f',
        )

    @staticmethod
    def _update_location(content, bucket_name):
        bucket_name = normalize_bucket_name(bucket_name)

        host = config.HOSTNAME_EXTERNAL
        if ':' not in host:
            host = '%s:%s' % (host, config.PORT_S3)
        return re.sub(r'<Location>\s*([a-zA-Z0-9\-]+)://[^/]+/([^<]+)\s*</Location>',
                      r'<Location>%s://%s/%s/\2</Location>' % (get_service_protocol(), host, bucket_name),
                      content, flags=re.MULTILINE)

    @staticmethod
    def is_query_allowable(method, query):
        # Generally if there is a query (some/path/with?query) we don't want to send notifications
        if not query:
            return True
        # Except we do want to notify on multipart and presigned url upload completion
        contains_cred = 'X-Amz-Credential' in query and 'X-Amz-Signature' in query
        contains_key = 'AWSAccessKeyId' in query and 'Signature' in query
        if (method == 'POST' and query.startswith('uploadId')) or contains_cred or contains_key:
            return True

    def forward_request(self, method, path, data, headers):

        # Create list of query parameteres from the url
        parsed = urlparse.urlparse('{}{}'.format(config.get_edge_url(), path))
        query_params = parse_qs(parsed.query)

        # Detecting pre-sign url and checking signature
        if any([p in query_params for p in PRESIGN_QUERY_PARAMS]):
            response = authenticate_presign_url(method=method, path=path, data=data, headers=headers)
            if response is not None:
                return response

        # parse path and query params
        parsed_path = urlparse.urlparse(path)

        # Make sure we use 'localhost' as forward host, to ensure moto uses path style addressing.
        # Note that all S3 clients using LocalStack need to enable path style addressing.
        if 's3.amazonaws.com' not in headers.get('host', ''):
            headers['host'] = 'localhost'

        # check content md5 hash integrity if not a copy request
        if 'Content-MD5' in headers and not self.is_s3_copy_request(headers, path):
            response = check_content_md5(data, headers)
            if response is not None:
                return response

        modified_data = None

        # check bucket name
        bucket_name = get_bucket_name(path, headers)
        if method == 'PUT' and not re.match(BUCKET_NAME_REGEX, bucket_name):
            if len(parsed_path.path) <= 1:
                return error_response('Unable to extract valid bucket name. Please ensure that your AWS SDK is ' +
                    'configured to use path style addressing, or send a valid <Bucket>.s3.amazonaws.com "Host" header',
                    'InvalidBucketName', status_code=400)

            return error_response('The specified bucket is not valid.', 'InvalidBucketName', status_code=400)

        # TODO: For some reason, moto doesn't allow us to put a location constraint on us-east-1
        to_find1 = to_bytes('<LocationConstraint>us-east-1</LocationConstraint>')
        to_find2 = to_bytes('<CreateBucketConfiguration')
        if data and data.startswith(to_bytes('<')) and to_find1 in data and to_find2 in data:
            # Note: with the latest version, <CreateBucketConfiguration> must either
            # contain a valid <LocationConstraint>, or not be present at all in the body.
            modified_data = b''

        # If this request contains streaming v4 authentication signatures, strip them from the message
        # Related isse: https://github.com/localstack/localstack/issues/98
        # TODO we should evaluate whether to replace moto s3 with scality/S3:
        # https://github.com/scality/S3/issues/237
        is_streaming_payload = headers.get(CONTENT_SHA256_HEADER) == STREAMING_HMAC_PAYLOAD
        if is_streaming_payload:
            modified_data = strip_chunk_signatures(not_none_or(modified_data, data))
            headers['Content-Length'] = headers.get('x-amz-decoded-content-length')

        # POST requests to S3 may include a "${filename}" placeholder in the
        # key, which should be replaced with an actual file name before storing.
        if method == 'POST':
            original_data = not_none_or(modified_data, data)
            expanded_data = multipart_content.expand_multipart_filename(original_data, headers)
            if expanded_data is not original_data:
                modified_data = expanded_data

        # If no content-type is provided, 'binary/octet-stream' should be used
        # src: https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectPUT.html
        if method == 'PUT' and not headers.get('content-type'):
            headers['content-type'] = 'binary/octet-stream'

        # parse query params
        query = parsed_path.query
        path = parsed_path.path
        bucket = path.split('/')[1]
        query_map = urlparse.parse_qs(query, keep_blank_values=True)

        # remap metadata query params (not supported in moto) to request headers
        append_metadata_headers(method, query_map, headers)

        # apply fixes
        headers_changed = fix_metadata_key_underscores(request_headers=headers)

        if query == 'notification' or 'notification' in query_map:
            # handle and return response for ?notification request
            response = handle_notification_request(bucket, method, data)
            return response

        # if the Expires key in the url is already expired then return error
        if method == 'GET' and 'Expires' in query_map:
            if is_url_already_expired(query_map.get('Expires')[0]):
                return token_expired_error(path, headers.get('x-amz-request-id'), 400)

        # If multipart POST with policy in the params, return error if the policy has expired
        if method == 'POST':
            policy_key, policy_value = multipart_content.find_multipart_key_value(data, headers, 'policy')
            if policy_key and policy_value:
                policy = json.loads(base64.b64decode(policy_value).decode('utf-8'))
                expiration_string = policy.get('expiration', None)  # Example: 2020-06-05T13:37:12Z
                if expiration_string:
                    expiration_datetime = datetime.datetime.strptime(expiration_string, POLICY_EXPIRATION_FORMAT)
                    expiration_timestamp = expiration_datetime.timestamp()
                    if is_url_already_expired(expiration_timestamp):
                        return token_expired_error(path, headers.get('x-amz-request-id'), 400)

        if query == 'cors' or 'cors' in query_map:
            if method == 'GET':
                return get_cors(bucket)
            if method == 'PUT':
                return set_cors(bucket, data)
            if method == 'DELETE':
                return delete_cors(bucket)

        if query == 'lifecycle' or 'lifecycle' in query_map:
            if method == 'GET':
                return get_lifecycle(bucket)
            if method == 'PUT':
                return set_lifecycle(bucket, data)

        if query == 'replication' or 'replication' in query_map:
            if method == 'GET':
                return get_replication(bucket)
            if method == 'PUT':
                return set_replication(bucket, data)

        if query == 'encryption' or 'encryption' in query_map:
            if method == 'GET':
                return get_encryption(bucket)
            if method == 'PUT':
                return set_encryption(bucket, data)

        if query == 'object-lock' or 'object-lock' in query_map:
            if method == 'GET':
                return get_object_lock(bucket)
            if method == 'PUT':
                return set_object_lock(bucket, data)

        if modified_data is not None or headers_changed:
            data_to_return = not_none_or(modified_data, data)
            if modified_data is not None:
                headers['Content-Length'] = str(len(data_to_return or ''))
            return Request(data=data_to_return, headers=headers, method=method)
        return True

    def get_forward_url(self, method, path, data, headers):
        def sub(match):
            # make sure to convert any bucket names to lower case
            bucket_name = normalize_bucket_name(match.group(1))
            return '/%s%s' % (bucket_name, match.group(2) or '')

        path_new = re.sub(r'/([^?/]+)([?/].*)?', sub, path)
        if path == path_new:
            return

        url = 'http://%s:%s%s' % (constants.LOCALHOST, PORT_S3_BACKEND, path_new)
        return url

    def return_response(self, method, path, data, headers, response, request_handler=None):
        path = to_str(path)
        method = to_str(method)
        # persist this API call to disk
        super(ProxyListenerS3, self).return_response(method, path, data, headers, response, request_handler)

        # No path-name based bucket name? Try host-based
        bucket_name = get_bucket_name(path, headers)
        hostname_parts = headers['host'].split('.')
        if (not bucket_name or len(bucket_name) == 0) and len(hostname_parts) > 1:
            bucket_name = hostname_parts[0]

        # POST requests to S3 may include a success_action_redirect or
        # success_action_status field, which should be used to redirect a
        # client to a new location.
        key = None
        if method == 'POST':
            key, redirect_url = multipart_content.find_multipart_key_value(data, headers)

            if key and redirect_url:
                response.status_code = 303
                response.headers['Location'] = expand_redirect_url(redirect_url, key, bucket_name)
                LOGGER.debug('S3 POST {} to {}'.format(response.status_code, response.headers['Location']))

            expanded_data = multipart_content.expand_multipart_filename(data, headers)
            key, status_code = multipart_content.find_multipart_key_value(
                expanded_data, headers, 'success_action_status'
            )

            if response.status_code == 201 and key:
                response._content = self.get_201_response(key, bucket_name)
                response.headers['Content-Length'] = str(len(response._content))
                response.headers['Content-Type'] = 'application/xml; charset=utf-8'
                return response
        if method == 'GET' and response.status_code == 416:
            return error_response('The requested range cannot be satisfied.', 'InvalidRange', 416)

        parsed = urlparse.urlparse(path)
        bucket_name_in_host = headers['host'].startswith(bucket_name)

        should_send_notifications = all([
            method in ('PUT', 'POST', 'DELETE'),
            '/' in path[1:] or bucket_name_in_host or key,
            # check if this is an actual put object request, because it could also be
            # a put bucket request with a path like this: /bucket_name/
            bucket_name_in_host or key or (len(path[1:].split('/')) > 1 and len(path[1:].split('/')[1]) > 0),
            self.is_query_allowable(method, parsed.query)
        ])

        # get subscribers and send bucket notifications
        if should_send_notifications:
            # if we already have a good key, use it, otherwise examine the path
            if key:
                object_path = '/' + key
            elif bucket_name_in_host:
                object_path = parsed.path
            else:
                parts = parsed.path[1:].split('/', 1)
                object_path = parts[1] if parts[1][0] == '/' else '/%s' % parts[1]
            version_id = response.headers.get('x-amz-version-id', None)

            send_notifications(method, bucket_name, object_path, version_id)

        # publish event for creation/deletion of buckets:
        if method in ('PUT', 'DELETE') and ('/' not in path[1:] or len(path[1:].split('/')[1]) <= 0):
            event_type = (event_publisher.EVENT_S3_CREATE_BUCKET if method == 'PUT'
                else event_publisher.EVENT_S3_DELETE_BUCKET)
            event_publisher.fire_event(event_type, payload={'n': event_publisher.get_hash(bucket_name)})

        # fix an upstream issue in moto S3 (see https://github.com/localstack/localstack/issues/382)
        if method == 'PUT' and parsed.query == 'policy':
            response._content = ''
            response.status_code = 204
            return response

        # emulate ErrorDocument functionality if a website is configured
        if method == 'GET' and response.status_code == 404 and parsed.query != 'website':
            s3_client = aws_stack.connect_to_service('s3')

            try:
                # Verify the bucket exists in the first place--if not, we want normal processing of the 404
                s3_client.head_bucket(Bucket=bucket_name)
                website_config = s3_client.get_bucket_website(Bucket=bucket_name)
                error_doc_key = website_config.get('ErrorDocument', {}).get('Key')

                if error_doc_key:
                    error_doc_path = '/' + bucket_name + '/' + error_doc_key
                    if parsed.path != error_doc_path:
                        error_object = s3_client.get_object(Bucket=bucket_name, Key=error_doc_key)
                        response.status_code = 200
                        response._content = error_object['Body'].read()
                        response.headers['Content-Length'] = str(len(response._content))
            except ClientError:
                # Pass on the 404 as usual
                pass

        if response is not None:
            reset_content_length = False
            # append CORS headers and other annotations/patches to response
            append_cors_headers(bucket_name, request_method=method, request_headers=headers, response=response)
            append_last_modified_headers(response=response)
            append_list_objects_marker(method, path, data, response)
            fix_location_constraint(response)
            fix_range_content_type(bucket_name, path, headers, response)
            fix_delete_objects_response(bucket_name, method, parsed, data, headers, response)
            fix_metadata_key_underscores(response=response)
            fix_creation_date(method, path, response=response)
            fix_etag_for_multipart(data, headers, response)
            ret304_on_etag(data, headers, response)
            append_aws_request_troubleshooting_headers(response)
            fix_delimiter(data, headers, response)

            if method == 'PUT':
                set_object_expiry(path, headers)

            # Remove body from PUT response on presigned URL
            # https://github.com/localstack/localstack/issues/1317
            if method == 'PUT' and response.status_code < 400 and ('X-Amz-Security-Token=' in path or
                    'X-Amz-Credential=' in path or 'AWSAccessKeyId=' in path):
                response._content = ''
                reset_content_length = True

            response_content_str = None
            try:
                response_content_str = to_str(response._content)
            except Exception:
                pass

            # Honor response header overrides
            # https://docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectGET.html
            if method == 'GET':
                add_accept_range_header(response)
                add_reponse_metadata_headers(response)
                if is_object_expired(path):
                    return no_such_key_error(path, headers.get('x-amz-request-id'), 400)

                query_map = urlparse.parse_qs(parsed.query, keep_blank_values=True)
                for param_name, header_name in ALLOWED_HEADER_OVERRIDES.items():
                    if param_name in query_map:
                        response.headers[header_name] = query_map[param_name][0]

            if response_content_str and response_content_str.startswith('<'):
                is_bytes = isinstance(response._content, six.binary_type)
                response._content = response_content_str

                append_last_modified_headers(response=response, content=response_content_str)

                # We need to un-pretty-print the XML, otherwise we run into this issue with Spark:
                # https://github.com/jserver/mock-s3/pull/9/files
                # https://github.com/localstack/localstack/issues/183
                # Note: yet, we need to make sure we have a newline after the first line: <?xml ...>\n
                # Note: make sure to return XML docs verbatim: https://github.com/localstack/localstack/issues/1037
                if method != 'GET' or not is_object_specific_request(path, headers):
                    response._content = re.sub(r'([^\?])>\n\s*<', r'\1><', response_content_str, flags=re.MULTILINE)

                # update Location information in response payload
                response._content = self._update_location(response._content, bucket_name)

                # convert back to bytes
                if is_bytes:
                    response._content = to_bytes(response._content)

                # fix content-type: https://github.com/localstack/localstack/issues/618
                #                   https://github.com/localstack/localstack/issues/549
                #                   https://github.com/localstack/localstack/issues/854
                if 'text/html' in response.headers.get('Content-Type', '') \
                        and not response_content_str.lower().startswith('<!doctype html'):
                    response.headers['Content-Type'] = 'application/xml; charset=utf-8'

                reset_content_length = True

            # update content-length headers (fix https://github.com/localstack/localstack/issues/541)
            if method == 'DELETE':
                reset_content_length = True

            if reset_content_length:
                response.headers['Content-Length'] = str(len(response._content))

            # convert to chunked encoding, for compatibility with certain SDKs (e.g., AWS PHP SDK)
            convert_to_chunked_encoding(method, path, response)

            if headers.get('Accept-Encoding') == 'gzip':
                if response._content is None:
                    response._content = ''
                response._content = gzip.compress(to_bytes(response._content))
                response.headers['Content-Length'] = str(len(response._content))
                response.headers['Content-Encoding'] = 'gzip'


def authenticate_presign_url(method, path, headers, data=None):

    sign_headers = []
    url = '{}{}'.format(config.get_edge_url(), path)
    parsed = urlparse.urlparse(url)
    query_params = parse_qs(parsed.query)

    # Checking required parameters are present in url or not
    if not all([p in query_params for p in PRESIGN_QUERY_PARAMS]):
        return requests_error_response_xml_signature_calculation(
            code=403,
            message='Query-string authentication requires the Signature, Expires and AWSAccessKeyId parameters',
            code_string='AccessDenied'
        )

    # Fetching headers which has been sent to the requets
    for header in headers:
        key = header[0]
        if key.lower() not in IGNORED_HEADERS_LOWER:
            sign_headers.append(header)

    # Request's headers are more essentials than the query parameters in the requets.
    # Different values of header in the header of the request and in the query paramter of the requets url
    # will fail the signature calulation. As per the AWS behaviour
    presign_params_lower = [p.lower() for p in PRESIGN_QUERY_PARAMS]
    if len(query_params) > 2:
        for key in query_params:
            if key.lower() not in presign_params_lower:
                if key.lower() not in (header[0].lower() for header in headers):
                    sign_headers.append((key, query_params[key][0]))

    # Preparnig dictionary of request to build AWSRequest's object of the botocore
    request_dict = {
        'url_path': path.split('?')[0],
        'query_string': {},
        'method': method,
        'headers': dict(sign_headers),
        'body': b'',
        'url': url.split('?')[0],
        'context': {
            'is_presign_request': True,
            'use_global_endpoint': True,
            'signing': {
                'bucket': str(path.split('?')[0]).split('/')[1]
            }
        }
    }
    aws_request = create_request_object(request_dict)

    # Calculating Signature
    credentials = Credentials(access_key=TEST_AWS_ACCESS_KEY_ID, secret_key=TEST_AWS_SECRET_ACCESS_KEY)
    auth = HmacV1QueryAuth(credentials=credentials, expires=query_params['Expires'][0])
    split = urlsplit(aws_request.url)
    string_to_sign = auth.get_string_to_sign(method=method, split=split, headers=aws_request.headers)
    signature = auth.get_signature(string_to_sign=string_to_sign)

    # Comparing the signature in url with signature we calculated
    if query_params['Signature'][0] != signature:
        return requests_error_response_xml_signature_calculation(
            code=403,
            code_string='SignatureDoesNotMatch',
            aws_access_token=TEST_AWS_ACCESS_KEY_ID,
            string_to_sign=string_to_sign,
            signature=signature,
            message='The request signature we calculated does not match the signature you provided. \
                    Check your key and signing method.')

    # Checking whether the url is expired or not
    if int(query_params['Expires'][0]) < time.time():
        return requests_error_response_xml_signature_calculation(
            code=403,
            code_string='AccessDenied',
            message='Request has expired',
            expires=query_params['Expires'][0]
        )


# instantiate listener
UPDATE_S3 = ProxyListenerS3()
