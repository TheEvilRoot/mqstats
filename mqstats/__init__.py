import datetime
import json
import logging
import socket
import platform
import sys

import psutil
import os

import paho.mqtt.client as mqtt

def from_env(key: str, default: str) -> str:
    """Get value from environment variable or return default if empty or unset"""
    value = os.environ.get(key, '')
    return default if value is None or len(value) == 0 else value

def list_from_env(key: str) -> list:
    """Get list value from environment variable.
       List is defined as string of comma separated values"""
    value = from_env(key, '')
    return [value.strip() for value in value.split(',') if len(value.strip()) > 0]

def int_from_env(key: str, default: int) -> int:
    """Get int value from environment variable or return default if empty or unset
       Throw an error when value is not an integer"""
    value = from_env(key, '')
    if value is None or len(value) == 0:
        return default
    try:
        return int(value)
    except ValueError:
        raise Exception(f'Value of {key} is not an integer: {value}')

MQSTATS_DEVICE_NAME = from_env('MQSTATS_DEVICE_NAME', socket.gethostname())
MQSTATS_BASE_TOPIC = from_env('MQSTATS_BASE_TOPIC', 'mqstats')

MQSTATS_MQTT_HOST = from_env('MQSTATS_MQTT_HOST', 'localhost')
MQSTATS_MQTT_PORT = int_from_env('MQSTATS_MQTT_PORT', 1883)
MQSTATS_MQTT_USERNAME = from_env('MQSTATS_MQTT_USERNAME', '')
MQSTATS_MQTT_PASSWORD = from_env('MQSTATS_MQTT_PASSWORD', '')

MQSTATS_SENSOR_INTERVAL = int_from_env('MQSTATS_SENSOR_INTERVAL', 2)
MQSTATS_NICS = list_from_env('MQSTATS_NICS')


def sanitize_id(name: str) -> str:
    """Make home assistant compatible identifier from name"""
    return name.lower().replace('-', '_').replace(' ', '_')

def device_id() -> str:
    """Device name as sanitised identifier"""
    return sanitize_id(MQSTATS_DEVICE_NAME)

def device_config():
    """Config with information about device"""
    return {
        'name': MQSTATS_DEVICE_NAME,
        'model': platform.machine(),
        'model_id': platform.machine(),
        'identifiers': [device_id()],
    }

def program_config():
    """Config with information about collection program"""
    return {
        'name': 'mqstats',
        'sw': '1.0',
        'url': 'https://github.com/TheEvilRoot/mqstats',
    }

def sensor_config(sensor_id: str, name: str, unit: str, template: str, precision: int = 2):
    """Create base of sensor configuration
       Use as { 'cmps': {**sensor_config('a'), **sensor_config('b')} } """
    return {sanitize_id(sensor_id): {
        'name': name,
        'platform': 'sensor',
        'state_class': 'measurement',
        'state_topic': f'{MQSTATS_BASE_TOPIC}/{device_id()}/{sanitize_id(sensor_id)}',
        'suggested_display_precision': precision,
        'unit_of_measurement': unit,
        'value_template': template,
        'unique_id': f'{device_id()}_{sanitize_id(sensor_id)}',
    }}

def discovery_config() -> dict:
    """Create discovery config for Home Assistant"""
    base = {'o': program_config(), 'dev': device_config(), 'cmps': {
        **sensor_config('cpu_percent', 'CPU%', '%', '{{ value_json.cpu_percent | round(1) }}', precision=1),
        **sensor_config('cpu_freq', 'CPU Frequency', 'MHz', '{{ value_json.cpu_freq | round(0) }}', precision=0),
        **sensor_config('memory_percent', 'Memory%', '%', '{{ value_json.memory_percent | round(0) }}', precision=0),
        **sensor_config('memory_used', 'Memory used', 'MB', '{{ value_json.memory_used / (1024 * 1024) | round(1) }}', precision=1),
        **sensor_config('memory_free', 'Memory free', 'MB', '{{ value_json.memory_free / (1024 * 1024) | round(1) }}', precision=1),
        **sensor_config('disk_percent', 'Disk%', '%', '{{ value_json.disk_used | round(1) }}', precision=1),
        **sensor_config('disk_free', 'Disk free', 'MB', '{{ value_json.disk_free / (1024 * 1024) | round(2) }}', precision=2),
    }}
    for nic in MQSTATS_NICS:
        base['cmps'] |= sensor_config(f'nic_{nic}_speed', f'{nic} link', 'Mbps', '{{ value_json.nic_speed | round(0) }}', precision=0)
        base['cmps'] |= sensor_config(f'nic_{nic}_upload', f'{nic} upload', 'Mbps', '{{ value_json.nic_upload / (1024 * 1024) | round(2) }}', precision=0)
        base['cmps'] |= sensor_config(f'nic_{nic}_download', f'{nic} download', 'Mbps', '{{ value_json.nic_download / (1024 * 1024) | round(2) }}', precision=0)
    return base

def send_message(client: mqtt.Client, topic: str, message: dict):
    """Send JSON message to topic"""
    if topic is not None:
        client.publish(topic, json.dumps(message))

def create_mqtt_client():
    def on_connect(*args, **kwargs):
        logging.info('Connected to MQTT broker')
    def on_connect_fail(*args, **kwargs):
        logging.info('Connection failed to MQTT broker')
    def on_disconnect(*args, **kwargs):
        logging.info(f'Disconnected from MQTT broker: {args} {kwargs}')
    def on_login(*args, **kwargs):
        logging.info('Connected to MQTT broker')
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if len(MQSTATS_MQTT_USERNAME) > 0:
        client.username_pw_set(MQSTATS_MQTT_USERNAME, MQSTATS_MQTT_PASSWORD)
    client.on_login = on_login
    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    client.on_disconnect = on_disconnect
    client.connect(MQSTATS_MQTT_HOST, MQSTATS_MQTT_PORT)
    client.loop_start()
    return client

def collection_handler(client: mqtt.Client):
    discovery = discovery_config()
    def find_topic(sensor_id: str) -> str:
        sensor = discovery['cmps'][sanitize_id(sensor_id)]
        return sensor['state_topic']

    discovery_topic = f'homeassistant/device/{device_id()}/config'
    send_message(client, discovery_topic, discovery)

    prev_counters = psutil.net_io_counters(pernic=True)
    while True:
        start = datetime.datetime.now()
        cpu_percent = psutil.cpu_percent(interval=MQSTATS_SENSOR_INTERVAL)
        cpu_freq = psutil.cpu_freq().current
        memory = psutil.virtual_memory()
        memory_used = memory.percent
        memory_free = memory.free
        disk_used = psutil.disk_usage('/').percent
        disk_free = psutil.disk_usage('/').free
        net_stats = psutil.net_if_stats()
        net_counters = psutil.net_io_counters(pernic=True)
        for nic in MQSTATS_NICS:
            nic_speed = net_stats[nic].speed
            download_speed = net_counters[nic].bytes_recv - prev_counters[nic].bytes_recv
            upload_speed = net_counters[nic].bytes_sent - prev_counters[nic].bytes_sent
            send_message(client, find_topic(f'nic_{nic}_download'), {'nic_download': download_speed})
            send_message(client, find_topic(f'nic_{nic}_upload'), {'nic_upload': upload_speed})
            send_message(client, find_topic(f'nic_{nic}_speed'), {'nic_speed': nic_speed})
        prev_counters = net_counters
        send_message(client, find_topic('cpu_percent'), {'cpu_percent': cpu_percent})
        send_message(client, find_topic('cpu_freq'), {'cpu_freq': cpu_freq})
        send_message(client, find_topic('memory_percent'), {'memory_percent': memory_used})
        send_message(client, find_topic('memory_used'), {'memory_used': memory_used})
        send_message(client, find_topic('memory_free'), {'memory_free': memory_free})
        send_message(client, find_topic('disk_percent'), {'disk_used': disk_used})
        send_message(client, find_topic('disk_free'), {'disk_free': disk_free})
        end = datetime.datetime.now()
        logging.info(f'Complete collection in {(end - start).total_seconds()} seconds')

def main():
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s', stream=sys.stdout)

    client = create_mqtt_client()
    collection_handler(client)

if __name__ == '__main__':
    main()
