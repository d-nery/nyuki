import logging

from slixmpp import ClientXMPP
from slixmpp.exceptions import XMPPError, IqError, IqTimeout

from nyuki.loop import EventLoop
from nyuki.event import EventManager, Event


log = logging.getLogger(__name__)


class _BusClient(ClientXMPP):
    """
    XMPP client to connect to the bus.
    This class is based on Slixmpp (fork of Sleexmpp) using asyncio event loop.
    """

    def __init__(self, jid, password, host=None, port=None):
        super().__init__(jid, password)
        try:
            host = host or jid.split('@')[1]
        except IndexError:
            raise XMPPError("Missing argument: host")
        self._address = (host, port or 5222)

        self.register_plugin('xep_0045')  # Multi-user chat
        self.register_plugin('xep_0133')  # Service administration
        self.register_plugin('xep_0077')  # In-band registration

        # Disable IPv6 util we really need it
        self.use_ipv6 = False

    def connect(self, **kwargs):
        """
        Schedule the connection process.
        """
        return super().connect(address=self._address, **kwargs)


class Bus(object):
    """
    Provide methods to perform communication over the bus.
    Events are handled by the EventManager.
    """
    def __init__(self, jid, password, host=None, port=None):
        self.client = _BusClient(jid, password, host, port)
        self.client.add_event_handler('connecting', self._on_connection)
        self.client.add_event_handler('register', self._on_register)
        self.client.add_event_handler('session_start', self._on_start)
        self.client.add_event_handler('message', self._on_message)
        self.client.add_event_handler('disconnected', self._on_disconnect)
        self.client.add_event_handler('connection_failed', self._on_failure)

        # Wrap asyncio loop for easy use
        self._loop = EventLoop(loop=self.client.loop)
        self._event = EventManager(self._loop)

    @property
    def loop(self):
        return self._loop

    @property
    def event_manager(self):
        return self._event

    def _on_connection(self, event):
        """
        XMPP event handler while starting the authentication to the server.
        Also trigger a bus event: `Connecting`.
        """
        log.debug("Connecting to the bus")
        self._event.trigger(Event.Connecting)

    def _on_register(self, event):
        """
        XMPP event handler while registering a user (in-band registration).
        Does not trigger bus event (internal behavior).
        """
        resp = self.client.Iq()
        resp['type'] = 'set'
        resp['register']['username'] = self.client.boundjid.user
        resp['register']['password'] = self.client.password

        try:
            yield from resp.send()
        except IqError as exc:
            error = exc.iq['error']['text']
            log.warning("Could not register account: {}".format(error))
        except IqTimeout:
            log.error("No response from the server")
            self.disconnect()
        else:
            log.debug("Account {} created".format(self.client.boundjid))

    def _on_start(self, event):
        """
        XMPP event handler when the connection has been made.
        Also trigger a bus event: `Connected`.
        """
        self.client.send_presence()
        self.client.get_roster()
        log.debug("Connection to the bus succeed")
        self._event.trigger(Event.Connected)

    def _on_message(self, event):
        """
        XMPP event handler when a message has been received.
        Also trigger a bus event: `MessageReceived`.
        """
        log.debug("Message received: {}".format(event))
        self._event.trigger(Event.MessageReceived, event)

    def _on_disconnect(self, event):
        """
        XMPP event handler when the client has been disconnected.
        Also trigger a bus event: `Disconnected`.
        """
        log.debug("Disconnected from the bus")
        self._event.trigger(Event.Disconnected)

    def _on_failure(self, event):
        """
        XMPP event handler when something is going wrong with the connection.
        Also trigger a bus event: `ConnectionError`.
        """
        log.error("Connection failed")
        self._event.trigger(Event.ConnectionError, event)
        self.client.abort()

    def connect(self):
        """
        Connect to the XMPP server and init pluggins.
        """
        self.client.connect()
        self.client.init_plugins()

    def disconnect(self, timeout=5):
        """
        Disconnect to the XMPP server.
        Abort after a timeout (in seconds) if the action is too long.
        """
        self.client.disconnect(wait=timeout)