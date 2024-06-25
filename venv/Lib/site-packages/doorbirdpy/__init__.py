"""Main DoorBirdPy module."""
import threading
import requests
import json
import re
import time
from urllib.parse import urlencode
from requests import Session

from doorbirdpy.schedule_entry import (
    DoorBirdScheduleEntry,
    DoorBirdScheduleEntryOutput,
    DoorBirdScheduleEntrySchedule,
)


class DoorBird(object):
    """Represent a doorbell unit."""

    _monitor_timeout = 45  # seconds to wait for a monitor update
    _monitor_max_failures = 4

    def __init__(self, ip, username, password, http_session: Session = None, secure=False, port=None):
        """
        Initializes the options for subsequent connections to the unit.

        :param ip: The IP address of the unit
        :param username: The username (with sufficient privileges) of the unit
        :param password: The password for the provided username
        :param secure: set to True to use https instead of http for URLs
        :param port: override the HTTP port (defaults to 443 if secure = True, otherwise 80)
        """
        self._ip = ip
        self._credentials = username, password
        self._http = http_session or Session()
        self._secure = secure

        if port:
            self._port = port
        else:
            self._port = 443 if self._secure else 80

        self._monitor_thread = None
        self._monitor_thread_should_exit = False

    def ready(self):
        """
        Test the connection to the device.

        :return: A tuple containing the ready status (True/False) and the HTTP
        status code returned by the device or 0 for no status
        """
        url = self._url("/bha-api/info.cgi", auth=True)
        try:
            response = self._http.get(url)
            data = response.json()
            code = data["BHA"]["RETURNCODE"]
            return int(code) == 1, int(response.status_code)
        except ValueError:
            return False, int(response.status_code)

    @property
    def live_video_url(self):
        """
        A multipart JPEG live video stream with the default resolution and
        compression as defined in the system configuration.

        :return: The URL of the stream
        """
        return self._url("/bha-api/video.cgi")

    @property
    def live_image_url(self):
        """
        A JPEG file with the default resolution and compression as
        defined in the system configuration.

        :return: The URL of the image
        """
        return self._url("/bha-api/image.cgi")

    def energize_relay(self, relay=1):
        """
        Energize a door opener/alarm output/etc relay of the device.

        :return: True if OK, False if not
        """
        data = self._get_json(self._url("/bha-api/open-door.cgi", {"r": relay}, auth=True))
        return int(data["BHA"]["RETURNCODE"]) == 1

    def turn_light_on(self):
        """
        Turn on the IR lights.

        :return: JSON
        """
        data = self._get_json(self._url("/bha-api/light-on.cgi", auth=True))
        code = data["BHA"]["RETURNCODE"]
        return int(code) == 1

    def history_image_url(self, index, event):
        """
        A past image stored in the cloud.

        :param index: Index of the history images, where 1 is the latest history image
        :return: The URL of the image.
        """
        return self._url("/bha-api/history.cgi", {"index": index, "event": event})

    def schedule(self):
        """
        Get schedule settings.

        :return: A list of DoorBirdScheduleEntry objects
        """
        data = self._get_json(self._url("/bha-api/schedule.cgi", auth=True))
        return DoorBirdScheduleEntry.parse_all(data)

    def get_schedule_entry(self, sensor, param=""):
        """
        Find the schedule entry that matches the provided sensor and parameter
        or create a new one that does if none exists.

        :return: A DoorBirdScheduleEntry
        """
        entries = self.schedule()

        for entry in entries:
            if entry.input == sensor and entry.param == param:
                return entry

        return DoorBirdScheduleEntry(sensor, param)

    def change_schedule(self, entry):
        """
        Add or replace a schedule entry.

        :param entry: A DoorBirdScheduleEntry object to replace on the device
        :return: A tuple containing the success status (True/False) and the HTTP response code
        """
        url = self._url("/bha-api/schedule.cgi", auth=True)
        response = self._http.post(
            url,
            body=json.dumps(entry.export),
            headers={"Content-Type": "application/json"},
        )
        return int(response.status_code) == 200, response.status_code

    def delete_schedule(self, event, param=""):
        """
        Delete a schedule entry.

        :param event: Event type (doorbell, motion, rfid, input)
        :param param: param value of schedule entry to delete
        :return: True if OK, False if not
        """
        url = self._url(
            "/bha-api/schedule.cgi",
            {"action": "remove", "input": event, "param": param},
            auth=True,
        )
        response = self._http.get(url)
        return int(response.status_code) == 200

    def _monitor_doorbird(self, on_event, on_error):
        """
        Method to use by the monitoring thread
        """
        url = self._url("/bha-api/monitor.cgi", {"ring": "doorbell,motionsensor"}, auth=True)
        states = {"doorbell": "L", "motionsensor": "L"}
        failures = 0

        while True:
            if self._monitor_thread_should_exit:
                return

            try:
                response = requests.get(url, stream=True, timeout=self._monitor_timeout)
                failures = 0  # reset the failure count on each successful response

                if response.encoding is None:
                    response.encoding = "utf-8"

                for line in response.iter_lines(decode_unicode=True):  # read until connection is closed
                    if self._monitor_thread_should_exit:
                        response.close()
                        return

                    match = re.match(r"(doorbell|motionsensor):(H|L)", line)
                    if match:
                        event, value = match.group(1), match.group(2)
                        if states[event] != value:
                            states[event] = value
                            if value == "H":
                                on_event(event)

            except Exception as e:
                if failures >= self._monitor_max_failures:
                    return on_error(e)

                failures += 1
                time.sleep(2**failures)

    def start_monitoring(self, on_event, on_error):
        """
        Start monitoring for doorbird events

        :param on_event: A callback function, which takes the event name as its only parameter.
        The possible events are "doorbell" and "motionsensor"
        :param on_error: An error function, which will be called with an error if the thread fails.
        """
        if self._monitor_thread:
            self.stop_monitoring()

        self._monitor_thread = threading.Thread(target=self._monitor_doorbird, args=(on_event, on_error))
        self._monitor_thread_should_exit = False
        self._monitor_thread.start()

    def stop_monitoring(self):
        """
        Stop monitoring for doorbird events
        """
        if not self._monitor_thread:
            return

        self._monitor_thread_should_exit = True
        self._monitor_thread.join(self._monitor_timeout + 1)
        self._monitor_thread = None

    def doorbell_state(self):
        """
        The current state of the doorbell.

        :return: True for pressed, False for idle
        """
        url = self._url("/bha-api/monitor.cgi", {"check": "doorbell"}, auth=True)
        response = self._http.get(url)
        response.raise_for_status()

        try:
            return int(response.text.split("=")[1]) == 1
        except IndexError:
            return False

    def motion_sensor_state(self):
        """
        The current state of the motion sensor.

        :return: True for motion, False for idle
        """
        url = self._url("/bha-api/monitor.cgi", {"check": "motionsensor"}, auth=True)
        response = self._http.get(url)
        response.raise_for_status()

        try:
            return int(response.text.split("=")[1]) == 1
        except IndexError:
            return False

    def info(self):
        """
        Get information about the device.

        :return: A dictionary of the device information:
        - FIRMWARE
        - BUILD_NUMBER
        - WIFI_MAC_ADDR (if the device is connected via WiFi)
        - RELAYS list (if firmware version >= 000108)
        - DEVICE-TYPE (if firmware version >= 000108)
        """
        url = self._url("/bha-api/info.cgi", auth=True)
        data = self._get_json(url)
        return data["BHA"]["VERSION"][0]

    def favorites(self):
        """
        Get all saved favorites.

        :return: dict, as defined by the API.
        Top level items will be the favorite types (http, sip),
        which each reference another dict that maps ID
        to a dict with title and value keys.
        """
        return self._get_json(self._url("/bha-api/favorites.cgi", auth=True))

    def change_favorite(self, fav_type, title, value, fav_id=None):
        """
        Add a new saved favorite or change an existing one.

        :param fav_type: sip or http
        :param title: Short description
        :param value: URL including protocol and credentials
        :param fav_id: The ID of the favorite, only used when editing existing favorites
        :return: successful, True or False
        """
        args = {"action": "save", "type": fav_type, "title": title, "value": value}

        if fav_id:
            args["id"] = int(fav_id)

        response = self._http.get(self._url("/bha-api/favorites.cgi", args, auth=True))
        return int(response.status_code) == 200

    def delete_favorite(self, fav_type, fav_id):
        """
        Delete a saved favorite.

        :param fav_type: sip or http
        :param fav_id: The ID of the favorite
        :return: successful, True or False
        """
        url = self._url(
            "/bha-api/favorites.cgi",
            {"action": "remove", "type": fav_type, "id": fav_id},
            auth=True,
        )

        response = self._http.get(url)
        return int(response.status_code) == 200

    def restart(self):
        """
        Restart the device.

        :return: successful, True or False
        """
        url = self._url("/bha-api/restart.cgi")
        response = self._http.get(url)
        return int(response.status_code) == 200

    @property
    def rtsp_live_video_url(self):
        """
        Live video request over RTSP.

        :return: The URL for the MPEG H.264 live video stream
        """
        return self._url("/mpeg/media.amp", port=554, protocol="rtsp")

    @property
    def rtsp_over_http_live_video_url(self):
        """
        Live video request using RTSP over HTTP.

        :return: The URL for the MPEG H.264 live video stream
        """
        return self._url("/mpeg/media.amp", port=8557, protocol="rtsp")

    @property
    def html5_viewer_url(self):
        """
        The HTML5 viewer for interaction from other platforms.

        :return: The URL of the viewer
        """
        return self._url("/bha-api/view.html")

    def _url(self, path, args=None, port=None, auth=True, protocol=None):
        """
        Create a URL for accessing the device.

        :param path: The endpoint to call
        :param args: A dictionary of query parameters
        :param port: The port to use (defaults to 80)
        :param auth: Set to False to remove the URL authentication
        :param protocol: Allow protocol override (defaults to "http")
        :return: The full URL
        """
        if not port:
            port = self._port

        if not protocol:
            protocol = "https" if self._secure else "http"

        query = urlencode(args) if args else ""

        if auth:
            template = "{}://{}@{}:{}{}"
            user = ":".join(self._credentials)
            url = template.format(protocol, user, self._ip, port, path)
        else:
            template = "{}://{}:{}{}"
            url = template.format(protocol, self._ip, port, path)

        if query:
            url = "{}?{}".format(url, query)

        return url

    def _get_json(self, url):
        """
        Perform a GET request to the given URL on the device.

        :param url: The full URL to the API call
        :return: The JSON-decoded data sent by the device
        """
        response = self._http.get(url)
        response.raise_for_status()
        return response.json()
