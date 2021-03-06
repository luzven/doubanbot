from __future__ import with_statement

from twisted.python import log
from twisted.internet import task, reactor
from twisted.words.xish import domish
from twisted.words.protocols.jabber.jid import JID
from wokkel.xmppim import MessageProtocol, PresenceClientProtocol, RosterClientProtocol
from wokkel.xmppim import AvailablePresence
from wokkel.client import XMPPHandler

import xmpp_commands
import config
import models
import scheduling

current_conn = None
presence_conn = None
roster_conn = None

class DoubanbotMessageProtocol(MessageProtocol):

    def connectionMade(self):
        log.msg("Connected!")

        global current_conn
        current_conn = self

        commands=xmpp_commands.all_commands
        self.commands={}
        for c in commands.values():
            self.commands[c.name] = c
            for a in c.aliases: 
                self.commands[a] = c
        log.msg("Loaded commands: ", `self.commands.keys()`)

        # Let the scheduler know we connected.
        scheduling.connected()

    def connectionLost(self, reason):
        log.msg("Disconnected!")
        global current_conn
        current_conn = None
        scheduling.disconnected()

    def typing_notification(self, jid):
        """Send a typing notification to the given jid."""

        msg = domish.Element((None, "message"))
        msg["to"] = jid
        msg["from"] = config.SCREEN_NAME
        msg.addElement(('jabber:x:event', 'x')).addElement("composing")

        self.send(msg)

    def send_plain(self, jid, content):
        msg = domish.Element((None, "message"))
        msg["to"] = jid
        msg["from"] = config.SCREEN_NAME
        msg["type"] = 'chat'
        msg.addElement("body", content=content)

        self.send(msg)

    def send_html(self, jid, body, html):
        msg = domish.Element((None, "message"))
        msg["to"] = jid
        msg["from"] = config.SCREEN_NAME
        msg["type"] = 'chat'
        html = u"<html xmlns='http://jabber.org/protocol/xhtml-im'><body xmlns='http://www.w3.org/1999/xhtml'>"+unicode(html)+u"</body></html>"
        msg.addElement("body", content=unicode(body))
        msg.addRawXml(unicode(html))
 
        self.send(msg)

    def get_user(self, msg, session):
        jid = JID(msg['from'])
        try:
            user = models.User.by_jid(jid.userhost(), session)
        except:
            log.msg("Getting user without the jid in the DB (%s)" % jid.full())
            user = models.User.update_status(jid.userhost(), None, session)
            self.subscribe(jid)
        return user;

    def onError(self, msg):
        log.msg("Error received for %s: %s" % (msg['from'], msg.toXml()))
        scheduling.unavailable_user(JID(msg['from']))

    def onMessage(self, msg):
        try:
            self.__onMessage(msg);
        except KeyError:
            log.err()

    def __onMessage(self, msg):
        if hasattr(msg, "type") and msg["type"] == 'chat' and hasattr(msg, "body") and msg.body:
            self.typing_notification(msg['from'])
            a=unicode(msg.body).strip().split(None, 1)
            args = a[1] if len(a) > 1 else None
            with models.Session() as session:
                try:
                    user = self.get_user(msg, session)
                except:
                    log.err()
                    return self.send_plain(msg['from'],
                        "Stupid error processing message, please try again.")
                cmd = self.commands.get(a[0].lower())
                if cmd:
                    log.msg("Command %s received from %s" % (a[0], user.jid))
                    cmd(user, self, args, session)
                else:
                    d = None
                    if user.auto_post:
                        d=self.commands['post']
                    elif a[0][0] == '@':
                        d=self.commands['post']
                    if d:
                        log.msg("Command post(auto) received from %s" % user.jid)
                        d(user, self, unicode(msg.body).strip(), session)
                    else:
                        self.send_plain(msg['from'],
                            "No such command: %s\n"
                            "Send 'help' for known commands\n"
                            "If you intended to post your message, "
                            "please start your message with 'post', or see "
                            "'help autopost'" % a[0])
                try:
                    session.commit()
                except:
                    log.err()
        else:
            log.msg("Non-chat/body message: %s" % msg.toXml())


class DoubanbotPresenceProtocol(PresenceClientProtocol):

    _users = -1

    def connectionMade(self):
        # send initial presence
        self._users=-1
        self.update_presence()

        global presence_conn
        presence_conn = self

    @models.wants_session
    def update_presence(self, session):
        try:
            users=session.query(models.User).count()
            if users != self._users:
                status = "Working for %s users, Type 'help' for available commands" %users
                self.available(None, None, {None: status}, config.PRIORITY)
                self._users = users
        except:
            log.err()

    # available with avatar stuff
    def available(self, entity=None, show=None, statuses=None, priority=0):
        presence = AvailablePresence(entity, show, statuses, priority)
        presence.addElement(('vcard-temp:x:update', 'x')).addElement("photo", content=config.AVATAR) 
        self.send(presence)

    # presence stuff
    def availableReceived(self, entity, show=None, statuses=None, priority=0):
        log.msg("Available from %s (%s, %s, pri=%s)" % (
            entity.full(), show, statuses, priority))
        if entity.userhost() == JID(config.SCREEN_NAME).userhost():
            return

        if priority >= 0 and show not in ['xa', 'dnd']:
            scheduling.available_user(entity)
        else:
            log.msg("Marking jid unavailable due to negative priority or "
                "being somewhat unavailable.")
            scheduling.unavailable_user(entity)
    
    def unavailableReceived(self, entity, statuses=None):
        log.msg("Unavailable from %s" % entity.full())
        scheduling.unavailable_user(entity)

    @models.wants_session
    def subscribedReceived(self, entity, session):
        """
        According to http://xmpp.org/internet-drafts/draft-saintandre-rfc3921bis-07.html#substates-in-subscribed
        presence of 'subscribed' should not be deliver to client
        """

        log.msg("Subscribed received from %s" % (entity.userhost()))

    def unsubscribedReceived(self, entity):
        """
        According to http://xmpp.org/internet-drafts/draft-saintandre-rfc3921bis-07.html#substates-in-unsubscribed
        presence of 'unsubscribed' should not be deliver to client
        """
             
        log.msg("Unsubscribed received from %s" % (entity.userhost()))

    @models.wants_session
    def subscribeReceived(self, entity, session):
        log.msg("Subscribe received from %s" % (entity.userhost()))
        self.subscribe(entity)
        self.subscribed(entity)

        try:
            u = models.User.update_status(entity.userhost(), 'subscribed', session)
        except:
            log.err()

        cnt = -1
        try:
            cnt = session.query(models.User).count()
        except:
            log.err() 
        msg = "New subscriber: %s ( %d )" % (entity.userhost(), cnt)
        global current_conn
        for a in config.ADMINS:
            current_conn.send_plain(a, msg)

        self.update_presence()

    def unsubscribeReceived(self, entity):
        """
        According to http://xmpp.org/internet-drafts/draft-saintandre-rfc3921bis-07.html#substates-in-unsubscribe
        presence of 'unsubscribe' should not be deliver to client
        """

        log.msg("Unsubscribe received from %s" % (entity.userhost()))

class DoubanbotRosterProtocol(RosterClientProtocol):

    def connectionMade(self):
        global roster_conn
        roster_conn = self

    def connectionInitialized(self):
        RosterClientProtocol.connectionInitialized(self)
        self.getRoster()

    def onRosterSet(self, item):
        if not item.subscriptionTo and not item.subscriptionFrom and not item.ask:
            log.msg("Subscription of %s is none" % item.jid.userhost()) 
            self.removeItem(item.jid)
            scheduling.unavailable_user(item.jid)
            try:
                u = models.User.update_status(item.jid.userhost(), 'unsubscribed')
            except:
                log.err()

    def onRosterRemove(self, entity):
        log.msg("Roster %s removed" % entity.userhost())
        cnt = -1
        msg = "Unsubscribed: %s" % entity.userhost()
        global current_conn
        for a in config.ADMINS:
            current_conn.send_plain(a, msg)
