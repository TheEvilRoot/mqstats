import datetime
import json
import logging
import socket
import platform
import sys
import time
from typing import Callable

import paho.mqtt.client
import psutil

import paho.mqtt.client as mqtt
from ha_mqtt_discovery import Discovery, Device, Sensor, sanitize_name
from mqstats.utils import *

MQSTATS_DEVICE_NAME = from_env('MQSTATS_DEVICE_NAME', socket.gethostname())
MQSTATS_BASE_TOPIC = from_env('MQSTATS_BASE_TOPIC', 'mqstats')

MQSTATS_MQTT_HOST = from_env('MQSTATS_MQTT_HOST', 'localhost')
MQSTATS_MQTT_PORT = int_from_env('MQSTATS_MQTT_PORT', 1883)
MQSTATS_MQTT_USERNAME = from_env('MQSTATS_MQTT_USERNAME', '')
MQSTATS_MQTT_PASSWORD = from_env('MQSTATS_MQTT_PASSWORD', '')

MQSTATS_SENSOR_INTERVAL = int_from_env('MQSTATS_SENSOR_INTERVAL', 2)
MQSTATS_DISCOVER_INTERVAL = int_from_env('MQSTATS_DISCOVER_INTERVAL', 10)
MQSTATS_NICS = list_from_env('MQSTATS_NICS')
MQSTATS_DISKS = map_from_env('MQSTATS_DISKS')
MQSTATS_SENSORS = map_from_env('MQSTATS_SENSORS')

class Tick:
    def __init__(self, client: paho.mqtt.client.Client):
        self.memory = psutil.virtual_memory()
        self.net_stats = psutil.net_if_stats()
        self.net_counters = psutil.net_io_counters(pernic=True)
        self.now = datetime.datetime.now()
        self.client = client

    def send_message(self, topic: str, message: dict):
        self.client.publish(topic, json.dumps(message))

class CpuPercent(Sensor):
    def __init__(self, device: Device):
        super().__init__(device, 'CPU%', None, '%', '{{ value_json.cpu_percent | round(1) }}', 'cpu_percent', 1)
        self.state_class = 'measurement'
        self.state = None

    def on_tick(self, tick: Tick):
        self.state = psutil.cpu_percent(interval=MQSTATS_SENSOR_INTERVAL)
        return tick.send_message(self.state_topic, {'cpu_percent': self.state})

class CpuFreq(Sensor):
    def __init__(self, device: Device):
        super().__init__(device, 'CPU Frequency', 'frequency', 'Mhz', '{{ value_json.cpu_freq | int }}', 'cpu_freq', 0)
        self.state_class = 'measurement'
        self.state = None

    def on_tick(self, tick: Tick):
        self.state = psutil.cpu_freq().current
        return tick.send_message(self.state_topic, {'cpu_freq': self.state})

class RamSensor(Sensor):
    def __init__(self, device: Device, name: str, sensor_id: str, getter: Callable):
        super().__init__(device, name, '%', None, f'{{{{ value_json.{sensor_id} | int }}}}', sensor_id, 0)
        self.state_class = 'measurement'
        self.getter = getter
        self.state = None

    def on_tick(self, tick: Tick):
        self.state = self.getter(tick.memory)
        return tick.send_message(self.state_topic, {self.sensor_id: self.state})

class DiskPercent(Sensor):
    def __init__(self, device: Device, disk_name: str, path: str):
        super().__init__(device, f'Disk {disk_name}%', '%', None, '{{ value_json.disk_percent | round(1) }}', f'disk_{sanitize_name(disk_name)}_percent', 1)
        self.state_class = 'measurement'
        self.path = path
        self.disk_name = disk_name
        self.state = None

    def on_tick(self, tick: Tick):
        disk_usage = psutil.disk_usage(self.path)
        self.state = disk_usage.percent
        return tick.send_message(self.state_topic, {'disk_percent': self.state})

class DiskFree(Sensor):
    def __init__(self, device: Device, disk_name: str, path: str):
        super().__init__(device, f'Disk {disk_name} free', 'MB', 'data_size', '{{ value_json.disk_free | round(2) }}', f'disk_{sanitize_name(disk_name)}_free', 2)
        self.state_class = 'measurement'
        self.disk_name = disk_name
        self.path = path
        self.state = None

    def on_tick(self, tick: Tick):
        disk_usage = psutil.disk_usage(self.path)
        self.state = disk_usage.free
        return tick.send_message(self.state_topic, {'disk_free': self.state})

class NicLink(Sensor):
    def __init__(self, device: Device, path: str):
        super().__init__(device, f'{path} link', 'Mbit/s', 'data_rate', '{{ value_json.nic_speed | int }}', f'nic_{path}_speed', 0)
        self.state_class = 'measurement'
        self.path = path
        self.state = None

    def on_tick(self, tick: Tick):
        self.state = tick.net_stats[self.path].speed
        return tick.send_message(self.state_topic, {'nic_speed': self.state})

class NicTrack(Sensor):
    def __init__(self, device: Device, field: str, path: str, getter: Callable):
        super().__init__(device, f'{path} {field}', 'Mbit/s', 'data_rate', f'{{{{ value_json.nic_{field} | int }}}}',
                         f'nic_{path}_{field}', 0)
        self.state_class = 'measurement'
        self.field = field
        self.path = path
        self.getter = getter
        self.state = None
        self.prev = None

    def on_tick(self, tick: Tick):
        if self.prev is None:
            self.prev = (tick.net_counters[self.path], tick.now)
            return None
        prev, prev_date = self.prev
        prev_delta = (tick.now - prev_date).total_seconds()
        self.state = ((self.getter(tick.net_counters[self.path]) - self.getter(prev)) * 8) / prev_delta
        self.prev = (tick.net_counters[self.path], tick.now)
        return tick.send_message(self.state_topic, {f'nic_{self.field}': self.state})


# fan_sensor_stats = psutil.sensors_fans() if hasattr(psutil._psplatform, "sensors_fans") else {}
# temp_sensor_stats = psutil.sensors_temperatures(False) if hasattr(psutil._psplatform, "sensors_temperatures") else {}

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
    discovery = Discovery(MQSTATS_BASE_TOPIC)
    discovery.name = 'mqstats'
    discovery.version = '1.2'
    discovery.url = 'https://github.com/TheEvilRoot/mqstats'
    device = Device(discovery, MQSTATS_DEVICE_NAME, platform.machine(), platform.machine())
    CpuPercent(device)
    CpuFreq(device)
    RamSensor(device, 'Memory%', 'memory_percent', lambda x: x.percent)
    RamSensor(device, 'Memory used', 'memory_used', lambda x: x.percent)
    RamSensor(device, 'Memory free', 'memory_free', lambda x: x.percent)

    for disk_name, disk_path in MQSTATS_DISKS.items():
        DiskPercent(device, disk_name, disk_path)
        DiskFree(device, disk_name, disk_path)

    for nic in MQSTATS_NICS:
        NicLink(device, nic)
        NicTrack(device, 'upload', nic, lambda x: x.bytes_sent)
        NicTrack(device, 'download', nic, lambda x: x.bytes_recv)

    discovery_time = datetime.datetime.now()
    while True:
        tick = Tick(client)
        for sensor in device.sensors.values():
            sensor.on_tick(tick)
        start = tick.now
        end = datetime.datetime.now()
        if abs((end - start).total_seconds() - MQSTATS_SENSOR_INTERVAL) > MQSTATS_SENSOR_INTERVAL:
            logging.warning(f'Complete collection in {(end - start).total_seconds()} seconds')
        if (end - discovery_time).total_seconds() >= MQSTATS_DISCOVER_INTERVAL:
            tick.send_message(discovery.discovery_topic(), discovery.build())
        time.sleep(0.1)

def main():
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s', stream=sys.stdout)
    client = create_mqtt_client()
    collection_handler(client)


if __name__ == '__main__':
    main()
