import abc
import six
import itertools
import random
import netaddr
import logging
import json
from bottle import Bottle, request, HTTPResponse, HTTPError

logger = logging.getLogger(__name__)
logging.basicConfig(format='%(asctime)s:%(levelname)s:%(name)s:%(message)s',
                    level=logging.DEBUG)

app = Bottle()

DICT_DummyNeutronDbSubnet = {}

def makeResponse(code, data, type):
    if type == "plain":
        r = HTTPResponse(status=code, body="{0}\n".format(data))
        r.set_header('Content-Type', 'text/plain')
    elif type == "json":
        body = json.dumps(data) + "\n"
        r = HTTPResponse(status=code, body=body)
        r.set_header('Content-Type', 'application/json')
    return r

class GatewayConflictWithAllocationPools(Exception):
    pass

class OverlappingAllocationPools(Exception):
    pass

class InvalidAllocationPool(Exception):
    pass

class OutOfBoundsAllocationPool(Exception):
    pass

class AddressCalculationFailure(Exception):
    pass

class InvalidAddressType(Exception):
    pass


@six.add_metaclass(abc.ABCMeta)
class AddressRequest(object):
    """Abstract base class for address requests"""

class SpecificAddressRequest(AddressRequest):
    """For requesting a specified address from IPAM"""
    def __init__(self, address):
        """
        :param address: The address being requested
        :type address: A netaddr.IPAddress or convertible to one.
        """
        super(SpecificAddressRequest, self).__init__()
        self._address = netaddr.IPAddress(address)

    @property
    def address(self):
        return self._address


class AnyAddressRequest(AddressRequest):
    """Used to request any available address from the pool."""


class PreferNextAddressRequest(AnyAddressRequest):
    """Used to request next available IP address from the pool."""


class AutomaticAddressRequest(SpecificAddressRequest):
    """Used to create auto generated addresses, such as EUI64"""
    EUI64 = 'eui64'

    def _generate_eui64_address(self, **kwargs):
        if set(kwargs) != set(['prefix', 'mac']):
            raise AddressCalculationFailure
        prefix = kwargs['prefix']
        mac_address = kwargs['mac']
        return get_ipv6_addr_by_EUI64(prefix, mac_address)

    _address_generators = {EUI64: _generate_eui64_address}

    def __init__(self, address_type=EUI64, **kwargs):
        """
        This constructor builds an automatic IP address. Parameter needed for
        generating it can be passed as optional keyword arguments.

        :param address_type: the type of address to generate.
            It could be an eui-64 address, a random IPv6 address, or
            an ipv4 link-local address.
            For the Kilo release only eui-64 addresses will be supported.
        """
        address_generator = self._address_generators.get(address_type)
        if not address_generator:
            raise InvalidAddressType
        address = address_generator(self, **kwargs)
        super(AutomaticAddressRequest, self).__init__(address)


class IpamSubnetManager(object):

    def __init__(self, neutron_subnet_id):
        self._neutron_subnet_id = neutron_subnet_id
        self._ip_allocations = []

    def list_allocations(self):
        return self._ip_allocations

    def create_allocation(self, ip_address):
        self._ip_allocations.append({'ip_address': ip_address})

    def delete_allocation(self, ip_address):
        self._ip_allocations.remove({'ip_address': ip_address})


class DummyNeutronDbSubnet(object):

    def __init__(self, subnet_id, allocation_pools, cidr, gateway_ip=None):

        self._cidr = cidr
        self._pools = allocation_pools
        self._subnet_id = subnet_id
        self.subnet_manager = IpamSubnetManager(self._subnet_id)

    def _verify_ip(self, ip_address):
        pass

    def _generate_ip(self, prefer_next=False):
        """Generate an IP address from the set of available addresses."""
        ip_allocations = netaddr.IPSet()
        for ipallocation in self.subnet_manager.list_allocations():
            ip_allocations.add(ipallocation['ip_address'])

        ip_set = netaddr.IPSet()
        for pool in self._pools:
            ip_set.add(pool)
        av_set = ip_set.difference(ip_allocations)

        if prefer_next:
            window = 1
        else:
            # Compute a value for the selection window
            window = min(av_set.size, 30)
        ip_index = random.randint(1, window)
        candidate_ips = list(itertools.islice(av_set, ip_index))
        allocated_ip = candidate_ips[
            random.randint(0, len(candidate_ips) - 1)]
        return str(allocated_ip)

    def allocate(self, address_request):
        all_pool_id = None
        if isinstance(address_request, SpecificAddressRequest):
            ip_address = str(address_request.address)
            self._verify_ip(ip_address)
        else:
            prefer_next = isinstance(address_request, PreferNextAddressRequest)
            ip_address = self._generate_ip(prefer_next)

        self.subnet_manager.create_allocation(ip_address)
        return ip_address

    def deallocate(self, address):
        self.subnet_manager.delete_allocation(address)

    def list_allocations(self):
        return self.subnet_manager.list_allocations()


def get_ipv6_addr_by_EUI64(prefix, mac):
    """Calculate IPv6 address using EUI-64 specification.

    This method calculates the IPv6 address using the EUI-64
    addressing scheme as explained in rfc2373.

    :param prefix: IPv6 prefix.
    :param mac: IEEE 802 48-bit MAC address.
    :returns: IPv6 address on success.
    :raises ValueError, TypeError: For any invalid input.

    .. versionadded:: 1.4
    """
    try:
        eui64 = int(netaddr.EUI(mac).eui64())
        prefix = netaddr.IPNetwork(prefix)
        return netaddr.IPAddress(prefix.first + eui64 ^ (1 << 57))
    except (ValueError, netaddr.AddrFormatError):
        raise ValueError(_('Bad prefix or mac format for generating IPv6 '
                           'address by EUI-64: %(prefix)s, %(mac)s:')
                         % {'prefix': prefix, 'mac': mac})
    except TypeError:
        raise TypeError(_('Bad prefix type for generating IPv6 address by '
                          'EUI-64: %s') % prefix)


def prepare_allocation_pools(allocation_pools, cidr, gateway_ip):
    """Returns allocation pools represented as list of IPRanges"""
    if not allocation_pools:
        return generate_pools(cidr, gateway_ip)

    ip_range_pools = pools_to_ip_range(allocation_pools)
    validate_allocation_pools(ip_range_pools, cidr)
    if gateway_ip:
        validate_gw_out_of_pools(gateway_ip, ip_range_pools)
    return ip_range_pools


def generate_pools(cidr, gateway_ip):
    """Create IP allocation pools for a specified subnet

    The Neutron API defines a subnet's allocation pools as a list of
    IPRange objects for defining the pool range.
    """
    # Auto allocate the pool around gateway_ip
    net = netaddr.IPNetwork(cidr)
    ip_version = net.version
    first = netaddr.IPAddress(net.first, ip_version)
    last = netaddr.IPAddress(net.last, ip_version)
    if first == last:
        # handle single address subnet case
        return [netaddr.IPRange(first, last)]
    first_ip = first + 1
    # last address is broadcast in v4
    last_ip = last - (ip_version == 4)
    if first_ip >= last_ip:
        # /31 lands here
        return []
    ipset = netaddr.IPSet(netaddr.IPRange(first_ip, last_ip))
    if gateway_ip:
        ipset.remove(netaddr.IPAddress(gateway_ip, ip_version))
    return list(ipset.iter_ipranges())


def pools_to_ip_range(ip_pools):
    ip_range_pools = []
    for ip_pool in ip_pools:
        try:
            ip_range_pools.append(netaddr.IPRange(ip_pool['start'],
                                                  ip_pool['end']))
        except netaddr.AddrFormatError:
            logging.info("Found invalid IP address in pool: "
                         "%(start)s - %(end)s:",
                     {'start': ip_pool['start'],
                      'end': ip_pool['end']})
            raise InvalidAllocationPool
    return ip_range_pools


def validate_allocation_pools(ip_pools, subnet_cidr):
    """Validate IP allocation pools.

    Verify start and end address for each allocation pool are valid,
    ie: constituted by valid and appropriately ordered IP addresses.
    Also, verify pools do not overlap among themselves.
    Finally, verify that each range fall within the subnet's CIDR.
    """
    subnet = netaddr.IPNetwork(subnet_cidr)
    subnet_first_ip = netaddr.IPAddress(subnet.first + 1)
    # last address is broadcast in v4
    subnet_last_ip = netaddr.IPAddress(subnet.last - (subnet.version == 4))

    logging.debug("Performing IP validity checks on allocation pools")
    ip_sets = []
    for ip_pool in ip_pools:
        start_ip = netaddr.IPAddress(ip_pool.first, ip_pool.version)
        end_ip = netaddr.IPAddress(ip_pool.last, ip_pool.version)
        if (start_ip.version != subnet.version or
                end_ip.version != subnet.version):
            logging.info("Specified IP addresses do not match "
                         "the subnet IP version")
            raise InvalidAllocationPool
        if start_ip < subnet_first_ip or end_ip > subnet_last_ip:
            logging.info("Found pool larger than subnet "
                         "CIDR:%(start)s - %(end)s",
                     {'start': start_ip, 'end': end_ip})
            raise OutOfBoundsAllocationPool
        # Valid allocation pool
        # Create an IPSet for it for easily verifying overlaps
        ip_sets.append(netaddr.IPSet(ip_pool.cidrs()))

    logging.debug("Checking for overlaps among allocation pools "
                  "and gateway ip")
    ip_ranges = ip_pools[:]

    # Use integer cursors as an efficient way for implementing
    # comparison and avoiding comparing the same pair twice
    for l_cursor in range(len(ip_sets)):
        for r_cursor in range(l_cursor + 1, len(ip_sets)):
            if ip_sets[l_cursor] & ip_sets[r_cursor]:
                l_range = ip_ranges[l_cursor]
                r_range = ip_ranges[r_cursor]
                logging.info("Found overlapping ranges: %(l_range)s and "
                             "%(r_range)s",
                         {'l_range': l_range, 'r_range': r_range})
                raise OverlappingAllocationPools


def validate_gw_out_of_pools(gateway_ip, pools):
    for pool_range in pools:
        if netaddr.IPAddress(gateway_ip) in pool_range:
            raise GatewayConflictWithAllocationPools


def ipam_allocate_ips(subnets):
    allocated = []

    for subnet in subnets['subnets']:
        subnet_id = subnet['id']
        allocation_pools = subnet['allocation_pools']
        cidr = subnet['cidr']
        gateway_ip = subnet['gateway_ip']
        mac_address = subnet['mac_address']
        device_owner = subnet['device_owner']
        ip_address = subnet['ip_address']

        if ip_address:
            ip_request = SpecificAddressRequest(ip_address)
        elif mac_address:
            ip_request = AutomaticAddressRequest(prefix=cidr, mac=mac_address)
        elif device_owner == "dhcp":
            ip_request = PreferNextAddressRequest()
        else:
            ip_request = AnyAddressRequest()

        if (subnet_id in DICT_DummyNeutronDbSubnet.keys()):
            ip_address = DICT_DummyNeutronDbSubnet[subnet_id].allocate(ip_request)
        else:
            allocation_pools = prepare_allocation_pools(allocation_pools, cidr, gateway_ip)
            ipam_subnet = DummyNeutronDbSubnet(subnet_id, allocation_pools, cidr, gateway_ip)
            ip_address = ipam_subnet.allocate(ip_request)
            DICT_DummyNeutronDbSubnet[subnet_id] = ipam_subnet

        allocated.append({'ip_address': ip_address, 'subnet_id': subnet_id})
    return allocated

def ipam_deallocate_ips(fixed_ips):
    deallocated = []

    for ip in fixed_ips:
        ipam_subnet = DICT_DummyNeutronDbSubnet[ip['subnet_id']]
        ipam_subnet.deallocate(ip['ip_address'])
        deallocated.append(ip)
    return deallocated

def get_ips(subnet_id):
    return DICT_DummyNeutronDbSubnet[subnet_id].list_allocations()


@app.post('/fixed_ips')
@app.post('/fixed_ips/')
def create_fixed_ip():
    logging.debug("### received post request={0}".format(request.json))
    request_info = request.json
    subnet_id = request_info['fixed_ip']['subnet_id']
    allocation_pools = request_info['fixed_ip']['allocation_pools']
    cidr = request_info['fixed_ip']['cidr']
    gateway_ip = request_info['fixed_ip']['gateway_ip']
    mac_address = request_info['fixed_ip']['mac_address']
    device_owner = request_info['fixed_ip']['device_owner']
    ip_address = request_info['fixed_ip']['ip_address']

    subnets_list = []
    subnets_list.append({'id': subnet_id,
                         'allocation_pools': allocation_pools,
                         'cidr': cidr,
                         'gateway_ip': gateway_ip,
                         'mac_address': mac_address,
                         'device_owner': device_owner,
                         'ip_address': ip_address})
    subnets = {'subnets': subnets_list}
    allocated = ipam_allocate_ips(subnets)
    fixed_ip_info = {'fixed_ip': allocated}
    return makeResponse(200, fixed_ip_info, "json")


@app.get('/fixed_ips')
@app.get('/fixed_ips/')
def get_fixed_ips():
    fixed_ips_body = []

    subnet_id = request.query.get("subnet_id")
    address_list = get_ips(subnet_id)

    for address in address_list:
        fixed_ips_body.append({'ip_address': address['ip_address'], 'subnet_id': subnet_id})

    fixed_ips_info = {'fixed_ips': fixed_ips_body}
    return makeResponse(200, fixed_ips_info, "json")

@app.delete('/fixed_ips')
@app.delete('/fixed_ips/')
def delete_fixed_ips():
    logging.debug("### received delete request={0}".format(request.json))
    request_info = request.json
    subnet_id = request_info['fixed_ip']['subnet_id']
    ip_address = request_info['fixed_ip']['ip_address']

    fixed_ips = []
    fixed_ips.append({'ip_address': ip_address, 'subnet_id': subnet_id})
    deallocated = ipam_deallocate_ips(fixed_ips)
    fixed_ip_info = {'fixed_ip': deallocated}
    return makeResponse(200, fixed_ip_info, "json")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
