from bmconfigparser import BMConfigParser
from helper_sql import sqlQuery

from foldertree import AccountMixin
from utils import str_broadcast_subscribers


def getSortedAccounts():
    configSections = BMConfigParser().addresses()
    configSections.sort(cmp = 
        lambda x,y: cmp(unicode(BMConfigParser().get(x, 'label'), 'utf-8').lower(), unicode(BMConfigParser().get(y, 'label'), 'utf-8').lower())
        )
    return configSections

def getSortedSubscriptions(count = False):
    queryreturn = sqlQuery('SELECT label, address, enabled FROM subscriptions ORDER BY label COLLATE NOCASE ASC')
    ret = {}
    for row in queryreturn:
        label, address, enabled = row
        ret[address] = {}
        ret[address]["inbox"] = {}
        ret[address]["inbox"]['label'] = label
        ret[address]["inbox"]['enabled'] = enabled
        ret[address]["inbox"]['count'] = 0
    if count:
        queryreturn = sqlQuery('''SELECT fromaddress, folder, count(msgid) as cnt
            FROM inbox, subscriptions ON subscriptions.address = inbox.fromaddress
            WHERE read = 0 AND toaddress = ?
            GROUP BY inbox.fromaddress, folder''', str_broadcast_subscribers)
        for row in queryreturn:
            address, folder, cnt = row
            if not folder in ret[address]:
                ret[address][folder] = {
                    'label': ret[address]['inbox']['label'],
                    'enabled': ret[address]['inbox']['enabled']
                }
            ret[address][folder]['count'] = cnt
    return ret

def accountClass(address):
    if not BMConfigParser().has_section(address):
        # FIXME: This BROADCAST section makes no sense
        if address == str_broadcast_subscribers:
            subscription = BroadcastAccount(address)
            if subscription.type != AccountMixin.BROADCAST:
                return None
        else:
            subscription = SubscriptionAccount(address)
            if subscription.type != AccountMixin.SUBSCRIPTION:
                # e.g. deleted chan
                return NoAccount(address)
        return subscription
    return BMAccount(address)
    
class AccountColor(AccountMixin):
    def __init__(self, address, type = None):
        self.isEnabled = True
        self.address = address
        if type is None:
            if address is None:
                self.type = AccountMixin.ALL
            elif BMConfigParser().safeGetBoolean(self.address, 'mailinglist'):
                self.type = AccountMixin.MAILINGLIST
            elif BMConfigParser().safeGetBoolean(self.address, 'chan'):
                self.type = AccountMixin.CHAN
            elif sqlQuery(
                '''select label from subscriptions where address=?''', self.address):
                self.type = AccountMixin.SUBSCRIPTION
            else:
                self.type = AccountMixin.NORMAL
        else:
            self.type = type

    
class BMAccount(object):
    def __init__(self, address = None):
        self.address = address
        self.type = AccountMixin.NORMAL
        if BMConfigParser().has_section(address):
            if BMConfigParser().safeGetBoolean(self.address, 'chan'):
                self.type = AccountMixin.CHAN
            elif BMConfigParser().safeGetBoolean(self.address, 'mailinglist'):
                self.type = AccountMixin.MAILINGLIST
        elif self.address == str_broadcast_subscribers:
            self.type = AccountMixin.BROADCAST
        else:
            queryreturn = sqlQuery(
                '''select label from subscriptions where address=?''', self.address)
            if queryreturn:
                self.type = AccountMixin.SUBSCRIPTION

    def getLabel(self, address = None):
        if address is None:
            address = self.address
        label = address
        if BMConfigParser().has_section(address):
            label = BMConfigParser().get(address, 'label')
        queryreturn = sqlQuery(
            '''select label from addressbook where address=?''', address)
        if queryreturn != []:
            for row in queryreturn:
                label, = row
        else:
            queryreturn = sqlQuery(
                '''select label from subscriptions where address=?''', address)
            if queryreturn != []:
                for row in queryreturn:
                    label, = row
        return label
        
    def parseMessage(self, toAddress, fromAddress, subject, message):
        self.toAddress = toAddress
        self.fromAddress = fromAddress
        if isinstance(subject, unicode):
            self.subject = str(subject)
        else:
            self.subject = subject
        self.message = message
        self.fromLabel = self.getLabel(fromAddress)
        self.toLabel = self.getLabel(toAddress)


class NoAccount(BMAccount):
    def __init__(self, address = None):
        self.address = address
        self.type = AccountMixin.NORMAL

    def getLabel(self, address = None):
        if address is None:
            address = self.address
        return address

        
class SubscriptionAccount(BMAccount):
    pass
    

class BroadcastAccount(BMAccount):
    pass
