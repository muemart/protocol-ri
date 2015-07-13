#!/usr/bin/env python3
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
##
# mPlane Software Development Kit
# Component framework
#
# (c) 2015 mPlane Consortium (http://www.ict-mplane.eu)
#     Author: Stefano Pentassuglia <stefano.pentassuglia@ssbprogetti.it>
#             Brian Trammell <brian@trammell.ch>
#
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.
#

import mplane.utils
import mplane.model
import mplane.azn
import mplane.tls
import importlib
import tornado.web
import tornado.httpserver
from datetime import datetime
import time
from time import sleep
import urllib3

# FIXME HACK
# some urllib3 versions let you disable warnings about untrusted CAs,
# which we use a lot in the project demo. Try to disable warnings if we can.
try:
    urllib3.disable_warnings()
except:
    pass

from threading import Thread
import json

DEFAULT_MPLANE_PORT = 1228
SLEEP_QUANTUM = 0.250
CAPABILITY_PATH_ELEM = "capability"
SPECIFICATION_PATH_ELEM = "/"

class BaseComponent(object):

    def __init__(self, config):
        self.config = config

        if config is not None:
            # preload any registries necessary
            if "Registries" in config:
                if "preload" in config["Registries"]:
                    for reg in config["Registries"]["preload"]:
                        mplane.model.preload_registry(reg)
                if "default" in config["Registries"]:
                    registry_uri = config["Registries"]["default"]
                else:
                    registry_uri = None
            else:
                registry_uri = None
        else:
            registry_uri = None

        # load default registry
        mplane.model.initialize_registry(registry_uri)

        self.tls = mplane.tls.TlsState(self.config)
        self.scheduler = mplane.scheduler.Scheduler(config)

        for service in self._services():
            if "Listener" in config["Component"]:
                if "interfaces" in config["Component"]["Listener"] and config["Component"]["Listener"]["interfaces"]:
                    if "TLS" in config:
                        link = "https://"
                    else:
                        link = "http://"
                    link = link + config["Component"]["Listener"]["interfaces"][0] + ":"
                    link = link + config["Component"]["Listener"]["port"] + SPECIFICATION_PATH_ELEM
                    service.set_capability_link(link)
                else:
                    service.set_capability_link("")
            self.scheduler.add_service(service)

    def _services(self):
        services = []
        if "Modules" in self.config["Component"]:
            for mod_name in self.config["Component"]["Modules"]:
                module = importlib.import_module(mod_name)
                kwargs = {}
                for arg in self.config["Component"]["Modules"][mod_name]:
                        kwargs[arg] = self.config["Component"]["Modules"][mod_name][arg]
                for service in module.services(**kwargs):
                    services.append(service)
        return services

    def remove_capability(self, capability):
        for service in self.scheduler.services:
            if service.capability().get_label() == capability.get_label():
                self.scheduler.remove_service(service)
                return
        print("No such service with label " + capability.get_label())

class ListenerHttpComponent(BaseComponent):

    def __init__(self, config, io_loop=None):
        self._port = DEFAULT_MPLANE_PORT
        if "port" in config["Component"]["Listener"]:
            self._port = int(config["Component"]["Listener"]["port"])

        self._path = SPECIFICATION_PATH_ELEM

        super(ListenerHttpComponent, self).__init__(config)

        application = tornado.web.Application([
            (r"/", MessagePostHandler, {'scheduler': self.scheduler, 'tlsState': self.tls}),
            (r"/"+CAPABILITY_PATH_ELEM, DiscoveryHandler, {'scheduler': self.scheduler, 'tlsState': self.tls}),
            (r"/"+CAPABILITY_PATH_ELEM+"/.*", DiscoveryHandler, {'scheduler': self.scheduler, 'tlsState': self.tls})
        ])
        http_server = tornado.httpserver.HTTPServer(
                        application,
                        ssl_options=self.tls.get_ssl_options())
        http_server.listen(self._port)
        print("ListenerHttpComponent running on port "+str(self._port))
        comp_t = Thread(target=self.listen_in_background, args=(io_loop,))
        comp_t.setDaemon(True)
        comp_t.start()

    def listen_in_background(self, io_loop):
        """ The component listens for requests in background """
        if io_loop is None:
            tornado.ioloop.IOLoop.instance().start()

class MPlaneHandler(tornado.web.RequestHandler):
    """
    Abstract tornado RequestHandler that allows a
    handler to respond with an mPlane Message.

    """
    def _respond_message(self, msg):
        self.set_status(200)
        self.set_header("Content-Type", "application/x-mplane+json")
        self.write(mplane.model.unparse_json(msg))
        self.finish()

class DiscoveryHandler(MPlaneHandler):
    """
    Exposes the capabilities registered with a given scheduler.
    URIs ending with "capability" will result in an HTML page
    listing links to each capability.

    """

    def initialize(self, scheduler, tlsState):
        self.scheduler = scheduler
        self.tls = tlsState

    def get(self):
        # capabilities
        path = self.request.path.split("/")[1:]
        if path[0] == CAPABILITY_PATH_ELEM:
            if (len(path) == 1 or path[1] is None):
                self._respond_capability_links()
            else:
                self._respond_capability(path[1])
        else:
            # FIXME how do we tell tornado we don't want to handle this?
            raise ValueError("I only know how to handle /"+CAPABILITY_PATH_ELEM+" URLs via HTTP GET")

    def _respond_capability_links(self):
        self.set_status(200)
        self.set_header("Content-Type", "text/html")
        self.write("<html><head><title>Capabilities</title></head><body>")
        no_caps_exposed = True
        for key in self.scheduler.capability_keys():
            if (not isinstance(self.scheduler.capability_for_key(key), mplane.model.Withdrawal) and
                    self.scheduler.azn.check(self.scheduler.capability_for_key(key),
                                             self.tls.extract_peer_identity(self.request))):
                no_caps_exposed = False
                self.write("<a href='/capability/" + key + "'>" + key + "</a><br/>")
        self.write("</body></html>")

        if no_caps_exposed is True:
                print("\nNo Capabilities are being exposed to " + self.tls.extract_peer_identity(self.request) +
                      ", check permissions in config file")
        self.finish()

    def _respond_capability(self, key):
        self._respond_message(self.scheduler.capability_for_key(key))

class MessagePostHandler(MPlaneHandler):
    """
    Receives mPlane messages POSTed from a client, and passes them to a
    scheduler for processing. After waiting for a specified delay to see
    if a Result is immediately available, returns a receipt for future
    redemption.

    """
    def initialize(self, scheduler, tlsState, immediate_ms = 5000):
        self.scheduler = scheduler
        self.tls = tlsState
        self.immediate_ms = immediate_ms

    def get(self):
        # message
        self.set_status(200)
        self.set_header("Content-Type", "text/html")
        self.write("<html><head><title>mplane.httpsrv</title></head><body>")
        self.write("This is an mplane.httpsrv instance. POST mPlane messages to this URL to use.<br/>")
        self.write("<a href='/"+CAPABILITY_PATH_ELEM+"'>Capabilities</a> provided by this server:<br/>")
        for key in self.scheduler.capability_keys():
            if (not isinstance(self.scheduler.capability_for_key(key), mplane.model.Withdrawal) and
                    self.scheduler.azn.check(self.scheduler.capability_for_key(key),
                                             self.tls.extract_peer_identity(self.request))):
                self.write("<br/><pre>")
                self.write(mplane.model.unparse_json(self.scheduler.capability_for_key(key)))
        self.write("</body></html>")
        self.finish()

    def post(self):
        # unwrap json message from body
        if (self.request.headers["Content-Type"] == "application/x-mplane+json"):
            msg = mplane.model.parse_json(self.request.body.decode("utf-8"))
        else:
            # FIXME how do we tell tornado we don't want to handle this?
            raise ValueError("I only know how to handle mPlane JSON messages via HTTP POST")

        is_withdrawn = False
        if isinstance(msg, mplane.model.Specification):
            for key in self.scheduler.capability_keys():
                cap = self.scheduler.capability_for_key(key)
                if msg.fulfills(cap) and isinstance(cap, mplane.model.Withdrawal):
                    is_withdrawn = True
                    self._respond_message(cap)

        if not is_withdrawn:
            # hand message to scheduler
            reply = self.scheduler.process_message(self.tls.extract_peer_identity(self.request), msg)

            # wait for immediate delay
            if self.immediate_ms > 0 and \
               isinstance(msg, mplane.model.Specification) and \
               isinstance(reply, mplane.model.Receipt):
                job = self.scheduler.job_for_message(reply)
                wait_start = datetime.utcnow()
                while (datetime.utcnow() - wait_start).total_seconds() * 1000 < self.immediate_ms:
                    time.sleep(SLEEP_QUANTUM)
                    if job.failed() or job.finished():
                        reply = job.get_reply()
                        break

            # return reply
            self._respond_message(reply)

class InitiatorHttpComponent(BaseComponent):

    def __init__(self, config, supervisor=False):
        self._supervisor = supervisor
        super(InitiatorHttpComponent, self).__init__(config)

        self.registration_url = urllib3.util.parse_url(self.config["Component"]["Initiator"]["capability-url"])
        self.specification_url = urllib3.util.parse_url(self.config["Component"]["Initiator"]["specification-url"])
        self.result_url = urllib3.util.parse_url(self.config["Component"]["Initiator"]["result-url"])

        self.pool = self.tls.pool_for(self.registration_url.scheme,
                                      self.registration_url.host,
                                      self.registration_url.port)
        self._result_url = dict()
        self.register_to_client()

        # periodically poll the Client/Supervisor for Specifications
        print("Checking for Specifications...")
        t = Thread(target=self.check_for_specs)
        t.start()

    def register_to_client(self, caps=None):
        """
        Sends a list of capabilities to the Client, in order to register them

        """
        env = mplane.model.Envelope()

        connected = False
        while not connected:
            try:
                self._client_identity = self.tls.extract_peer_identity(self.registration_url)
                connected = True
            except:
                print("Client/Supervisor unreachable. Retrying connection in 5 seconds")
                sleep(5)

        # If caps is not None, register that
        if caps is not None:
            for cap in caps:
                if self.scheduler.azn.check(cap, self._client_identity):
                    env.append_message(cap)
        else:
            # generate the envelope containing the capability list
            no_caps_exposed = True
            for key in self.scheduler.capability_keys():
                cap = self.scheduler.capability_for_key(key)
                if self.scheduler.azn.check(cap, self._client_identity):
                    env.append_message(cap)
                    no_caps_exposed = False

            if no_caps_exposed is True:
                print("\nNo Capabilities are being exposed to " + self._client_identity +
                      ", check permissions in config file. Exiting")
                if self._supervisor is False:
                    exit(0)

            # add callback capability to the list
            callback_cap = mplane.model.Capability(label="callback", when = "now ... future")
            env.append_message(callback_cap)

        # send the envelope to the client
        res = self.send_message(self.registration_url, "POST", env)

        # handle response message
        if res.status == 200:
            body = json.loads(res.data.decode("utf-8"))
            print("\nCapability registration outcome:")
            for key in body:
                if body[key]['registered'] == "ok":
                    print(key + ": Ok")
                else:
                    print(key + ": Failed (" + body[key]['reason'] + ")")
            print("")
        else:
            print("Error registering capabilities, Client/Supervisor said: " + str(res.status) + " - " + res.data.decode("utf-8"))
            exit(1)

    def check_for_specs(self):
        """
        Poll the client for specifications

        """
        while True:
            # FIXME configurable default idle time.
            self.idle_time = 5

            # send a request for specifications
            try:
                res = self.send_message(self.specification_url, "GET")
            except:
                print("Supervisor down. Trying to re-register...")
                self.register_to_client()

            if res.status == 200:

                # specs retrieved: split them if there is more than one
                env = mplane.model.parse_json(res.data.decode("utf-8"))
                for spec in env.messages():
                    # handle callbacks
                    if spec.get_label() == "callback":
                        self.idle_time = spec.when().timer_delays()[1]
                        break

                    # hand spec to scheduler
                    reply = self.scheduler.process_message(self._client_identity, spec, callback=self.return_results)
                    if not isinstance(spec, mplane.model.Interrupt):
                        self._result_url[spec.get_token()] = spec.get_link()

                    # send receipt to the Client/Supervisor
                    res = self.send_message(self._result_url[spec.get_token()], "POST", reply)

            # not registered on supervisor, need to re-register
            elif res.status == 428:
                print("\nRe-registering capabilities on Client/Supervisor")
                self.register_to_client()

            sleep(self.idle_time)

    def return_results(self, receipt):
        """
        Checks if a job is complete, and in case sends it to the Client/Supervisor

        """
        job = self.scheduler.job_for_message(receipt)
        reply = job.get_reply()

        # check if job is completed
        if (job.finished() is not True and
            job.failed() is not True):
            return

        res = self.send_message(self._result_url[reply.get_token()], "POST", reply)

        # handle response
        if isinstance(reply, mplane.model.Envelope):
            for msg in reply.messages():
                label = msg.get_label()
                break
        else:
            label = reply.get_label()
            if isinstance(reply, mplane.model.Exception):
                print("Exception for " + reply.get_token() + " successfully returned!")
                return

        if res.status == 200:
            print("Result for " + label + " successfully returned!")
        else:
            print("Error returning Result for " + label)
            print("Client/Supervisor said: " + str(res.status) + " - " + res.data.decode("utf-8"))
        pass

    def send_message(self, url_or_str, method, msg=None):
        url = url_or_str
        if isinstance(url_or_str, str):
            url = urllib3.util.parse_url(url_or_str)

        if self.pool.is_same_host(mplane.utils.unparse_url(url)):
            pool = self.pool
        else:
            pool = self.tls.pool_for(url.scheme, url.host, url.port)

        if method == "POST" and msg is not None:
            # post message
            res = pool.urlopen('POST', url.path,
                               body=mplane.model.unparse_json(msg).encode("utf-8"),
                               headers={"content-type": "application/x-mplane+json"})
        elif method == "GET":
            # get message
            res = pool.request('GET', url.path)

        return res

    def remove_capability(self, capability):
        super(InitiatorHttpComponent, self).remove_capability(capability)
        withdrawn_cap = mplane.model.Withdrawal(capability=capability)
        self.register_to_client([withdrawn_cap])
