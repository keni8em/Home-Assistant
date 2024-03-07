"""Code shared between all platforms."""
import asyncio
import logging
import time
from datetime import timedelta
from typing import Callable, Coroutine, NamedTuple

from homeassistant.core import HomeAssistant, CALLBACK_TYPE, callback
from homeassistant.config_entries import ConfigEntry

from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_DEVICE_CLASS,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_HOST,
    CONF_ID,
    CONF_PLATFORM,
    CONF_SCAN_INTERVAL,
    STATE_UNKNOWN,
    CONF_ENTITY_CATEGORY,
    EntityCategory,
    CONF_TYPE,
    CONF_ICON,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import async_track_time_interval, async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud_api import TuyaCloudApi
from .core import pytuya
from .const import (
    ATTR_STATE,
    ATTR_UPDATED_AT,
    CONF_DEFAULT_VALUE,
    CONF_ENABLE_DEBUG,
    CONF_NODE_ID,
    CONF_LOCAL_KEY,
    CONF_MODEL,
    CONF_PASSIVE_ENTITY,
    CONF_PROTOCOL_VERSION,
    CONF_RESET_DPIDS,
    CONF_RESTORE_ON_RECONNECT,
    DOMAIN,
    DEFAULT_CATEGORIES,
    ENTITY_CATEGORY,
    CONF_GATEWAY_ID,
    CONF_SCALING,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    domain,
    entity_class,
    flow_schema,
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up a Tuya platform based on a config entry.

    This is a generic method and each platform should lock domain and
    entity_class with functools.partial.
    """
    entities = []
    hass_entry_data: HassLocalTuyaData = hass.data[DOMAIN][config_entry.entry_id]

    for dev_id in config_entry.data[CONF_DEVICES]:
        dev_entry: dict = config_entry.data[CONF_DEVICES][dev_id]

        host = dev_entry.get(CONF_HOST)
        node_id = dev_entry.get(CONF_NODE_ID)
        device_key = f"{host}_{node_id}" if node_id else host

        entities_to_setup = [
            entity
            for entity in dev_entry[CONF_ENTITIES]
            if entity[CONF_PLATFORM] == domain
        ]

        if entities_to_setup:
            device: TuyaDevice = hass_entry_data.devices[device_key]
            dps_config_fields = list(get_dps_for_platform(flow_schema))

            for entity_config in entities_to_setup:
                # Add DPS used by this platform to the request list
                for dp_conf in dps_config_fields:
                    if dp_conf in entity_config:
                        device.dps_to_request[entity_config[dp_conf]] = None

                entities.append(
                    entity_class(
                        device,
                        dev_entry,
                        entity_config[CONF_ID],
                    )
                )
    # Once the entities have been created, add to the TuyaDevice instance
    device.add_entities(entities)
    async_add_entities(entities)


def get_dps_for_platform(flow_schema):
    """Return config keys for all platform keys that depends on a datapoint."""
    for key, value in flow_schema(None).items():
        if hasattr(value, "container") and value.container is None:
            yield key.schema


def get_entity_config(config_entry, dp_id) -> dict:
    """Return entity config for a given DPS id."""
    for entity in config_entry[CONF_ENTITIES]:
        if entity[CONF_ID] == dp_id:
            return entity
    raise Exception(f"missing entity config for id {dp_id}")


@callback
def async_config_entry_by_device_id(hass: HomeAssistant, device_id):
    """Look up config entry by device id."""
    current_entries = hass.config_entries.async_entries(DOMAIN)
    for entry in current_entries:
        if device_id in entry.data[CONF_DEVICES]:
            return entry
        # Search for gateway_id
        for dev_conf in entry.data[CONF_DEVICES].values():
            if (gw_id := dev_conf.get(CONF_GATEWAY_ID)) and gw_id == device_id:
                return entry
    return None


class TuyaDevice(pytuya.TuyaListener, pytuya.ContextualLogger):
    """Cache wrapper for pytuya.TuyaInterface."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        dev_id: str,
        fake_gateway=False,
    ):
        """Initialize the cache."""
        super().__init__()
        self._hass = hass
        self._hass_entry: HassLocalTuyaData = None
        self._config_entry = config_entry
        self._device_config: dict = config_entry.data[CONF_DEVICES][dev_id].copy()
        self._interface = None
        self._connect_max_tries = 3
        # For SubDevices
        self._node_id: str = self._device_config.get(CONF_NODE_ID)
        self._fake_gateway = fake_gateway
        self._gwateway: TuyaDevice = None
        self._sub_devices: dict[str, TuyaDevice] = {}

        self._status = {}
        self.dps_to_request = {}
        self._is_closing = False
        self._connect_task: asyncio.Task | None = None
        self._disconnect_task: Callable[[], None] | None = None
        self._unsub_interval: Callable[[], None] = None
        self._entities = []
        self._local_key: str = self._device_config[CONF_LOCAL_KEY]
        self._default_reset_dpids: list | None = None
        if reset_dps := self._device_config.get(CONF_RESET_DPIDS):
            self._default_reset_dpids = [int(id.strip()) for id in reset_dps.split(",")]

        self.set_logger(
            _LOGGER,
            self._device_config.get(CONF_DEVICE_ID),
            self._device_config.get(CONF_ENABLE_DEBUG),
        )

        # This has to be done in case the device type is type_0d
        for entity in self._device_config[CONF_ENTITIES]:
            self.dps_to_request[entity[CONF_ID]] = None

    def add_entities(self, entities):
        """Set the entities associated with this device."""
        self._entities.extend(entities)

    @property
    def is_connecting(self):
        """Return whether device is currently connecting."""
        return self._connect_task is not None

    @property
    def connected(self):
        """Return if connected to device."""
        return self._interface is not None

    @property
    def is_subdevice(self):
        """Return whether this is a subdevice or not."""
        return self._node_id and not self._fake_gateway

    async def get_gateway(self):
        """Return the gateway device of this sub device."""
        if not self._node_id:
            return
        gateway: TuyaDevice
        node_host = self._device_config.get(CONF_HOST)
        devices: dict = self._hass_entry.devices

        # Sub to gateway.
        if gateway := devices.get(node_host):
            self._gwateway = gateway
            gateway._sub_devices[self._node_id] = self
            return gateway
        else:
            self.error(f"Couldn't find the gateway for: {self._node_id}")
        return None

    async def async_connect(self, _now=None) -> None:
        """Connect to device if not already connected."""
        if not self._hass_entry:
            self._hass_entry = self._hass.data[DOMAIN][self._config_entry.entry_id]

        if not self._is_closing and not self.is_connecting and not self.connected:
            try:
                self._connect_task = self._hass.async_create_task(
                    self._make_connection()
                )
                await self._connect_task
            except (TimeoutError, asyncio.CancelledError):
                ...

    async def _make_connection(self):
        """Subscribe localtuya entity events."""
        name = self._device_config.get(CONF_FRIENDLY_NAME)
        host = name if self.is_subdevice else self._device_config.get(CONF_HOST)
        retry = 0
        self.debug(f"Trying to connect to {host}...", force=True)
        while retry < self._connect_max_tries:
            retry += 1
            try:
                if self.is_subdevice:
                    gateway = await self.get_gateway()
                    if not gateway or (not gateway.connected and gateway.is_connecting):
                        return await self.abort_connect()
                    self._interface = gateway._interface
                else:
                    self._interface = await pytuya.connect(
                        self._device_config[CONF_HOST],
                        self._device_config[CONF_DEVICE_ID],
                        self._local_key,
                        float(self._device_config[CONF_PROTOCOL_VERSION]),
                        self._device_config.get(CONF_ENABLE_DEBUG, False),
                        self,
                    )
                self._interface.add_dps_to_request(self.dps_to_request)
                break  # Succeed break while loop
            except Exception as ex:  # pylint: disable=broad-except
                if not retry < self._connect_max_tries:
                    self.warning(f"Failed to connect to {host}: {str(ex)}")
                await self.abort_connect()

        if self._interface is not None:
            try:
                # If reset dpids set - then assume reset is needed before status.
                reset_dpids = self._default_reset_dpids
                if (reset_dpids is not None) and (len(reset_dpids) > 0):
                    self.debug(f"Resetting cmd for DP IDs: {reset_dpids}")
                    # Assume we want to request status updated for the same set of DP_IDs as the reset ones.
                    self._interface.set_updatedps_list(reset_dpids)

                    # Reset the interface
                    await self._interface.reset(reset_dpids, cid=self._node_id)

                self.debug("Retrieving initial state")

                # Usually we use status instead of detect_available_dps, but some device doesn't reports all dps when ask for status.
                status = await self._interface.detect_available_dps(cid=self._node_id)
                if status is None:  # and not self.is_subdevice
                    raise Exception("Failed to retrieve status")
                self._interface.start_heartbeat()
                self.status_updated(status)

            except UnicodeDecodeError as e:
                self.exception(f"Connect to {host} failed: due to: {type(e)}")
                await self.abort_connect()
            except pytuya.DecodeError as derror:
                self.info(f"Initial update state failed {derror}, trying key update")
                await self.abort_connect()
                await self.update_local_key()
            except Exception as e:
                if not (self._fake_gateway and "Not found" in str(e)):
                    e = "Sub device is not connected" if self.is_subdevice else e
                    self.warning(f"Connect to {host} failed: {e}")
                    await self.abort_connect()
            except:
                if self._fake_gateway:
                    self.warning(f"Failed to use {name} as gateway.")
                    await self.abort_connect()

        if self._interface is not None:
            # Attempt to restore status for all entities that need to first set
            # the DPS value before the device will respond with status.
            for entity in self._entities:
                await entity.restore_state_when_connected()

            def _new_entity_handler(entity_id):
                self.debug(f"New entity {entity_id} was added to {host}")
                self._dispatch_status()

            signal = f"localtuya_entity_{self._device_config[CONF_DEVICE_ID]}"
            self._disconnect_task = async_dispatcher_connect(
                self._hass, signal, _new_entity_handler
            )

            if (scan_inv := int(self._device_config.get(CONF_SCAN_INTERVAL, 0))) > 0:
                self._unsub_interval = async_track_time_interval(
                    self._hass, self._async_refresh, timedelta(seconds=scan_inv)
                )

            self._is_closing = False
            self._connect_task = None
            self.debug(f"Success: connected to {host}", force=True)
            if self._sub_devices:
                connect_sub_devices = [
                    device.async_connect() for device in self._sub_devices.values()
                ]
                await asyncio.gather(*connect_sub_devices)

        self._connect_task = None

    async def abort_connect(self):
        """Abort the connect process to the interface[device]"""
        if self.is_subdevice:
            self._interface = None
            self._connect_task = None

        if self._interface is not None:
            await self._interface.close()
            self._interface = None

    async def check_connection(self):
        """Ensure that the device is not still connecting; if it is, wait for it."""
        if not self.connected and self._connect_task:
            await self._connect_task
        if not self.connected and self._gwateway and self._gwateway._connect_task:
            await self._gwateway._connect_task

    async def close(self):
        """Close connection and stop re-connect loop."""
        self._is_closing = True
        if self._connect_task is not None:
            self._connect_task.cancel()
            await self._connect_task
            self._connect_task = None
        if self._interface is not None:
            await self._interface.close()
            self._interface = None
        if self._disconnect_task is not None:
            self._disconnect_task()
        self.debug(
            f"Closed connection with {self._device_config[CONF_FRIENDLY_NAME]}",
            force=True,
        )

    async def update_local_key(self):
        """Retrieve updated local_key from Cloud API and update the config_entry."""
        dev_id = self._device_config[CONF_DEVICE_ID]
        cloud_api = self._hass_entry.cloud_data
        await cloud_api.async_get_devices_list()
        cloud_devs = cloud_api.device_list
        if dev_id in cloud_devs:
            cloud_localkey = cloud_devs[dev_id].get(CONF_LOCAL_KEY)
            if not cloud_localkey or self._local_key == cloud_localkey:
                return
            self._local_key = cloud_localkey
            new_data = self._config_entry.data.copy()
            new_data[CONF_DEVICES][dev_id][CONF_LOCAL_KEY] = self._local_key
            new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))
            self._hass.config_entries.async_update_entry(
                self._config_entry,
                data=new_data,
            )
            self.info(f"local_key updated for device {dev_id}.")

    async def _async_refresh(self, _now):
        if self._interface is not None:
            self.debug("Refreshing dps for device")
            await self._interface.update_dps(cid=self._node_id)

    async def set_dp(self, state, dp_index):
        """Change value of a DP of the Tuya device."""
        await self.check_connection()
        if self._interface is not None:
            try:
                await self._interface.set_dp(state, dp_index, cid=self._node_id)
            except Exception:  # pylint: disable=broad-except
                self.debug(f"Failed to set DP {dp_index} to {str(state)}", force=True)
        else:
            self.error(
                f"Not connected to device {self._device_config[CONF_FRIENDLY_NAME]}"
            )

    async def set_dps(self, states):
        """Change value of a DPs of the Tuya device."""
        await self.check_connection()
        if self._interface is not None:
            try:
                await self._interface.set_dps(states, cid=self._node_id)
            except Exception:  # pylint: disable=broad-except
                self.debug(f"Failed to set DPs {states}")
        else:
            self.error(
                f"Not connected to device {self._device_config[CONF_FRIENDLY_NAME]}"
            )

    def _dispatch_status(self):
        signal = f"localtuya_{self._device_config[CONF_DEVICE_ID]}"
        async_dispatcher_send(self._hass, signal, self._status)

    def _handle_event(self, old_status: dict, new_status: dict, deviceID=None):
        """Handle events in HA when devices updated."""

        def fire_event(event, data: dict):
            event_data = {
                CONF_DEVICE_ID: deviceID or self._device_config[CONF_DEVICE_ID]
            }
            event_data.update(data)
            # Send an event with status, The default length of event without data is 2.
            if len(event_data) > 2:
                self._hass.bus.async_fire(f"localtuya_{event}", event_data)

        event = "states_update"
        device_triggered = "device_triggered"
        device_dp_triggered = "device_dp_triggered"

        # Device Initializing. if not old_states.
        # States update event.
        if old_status and old_status != new_status:
            data = {"old_states": old_status, "new_states": new_status}
            fire_event(event, data)

        # Device triggered event.
        if old_status and new_status is not None:
            event = device_triggered
            data = {"states": old_status.update(new_status)}
            fire_event(event, data)

            if self._interface is not None:
                if len(self._interface.dispatched_dps) == 1:
                    event = device_dp_triggered
                    dpid_trigger = list(self._interface.dispatched_dps)[0]
                    dpid_value = self._interface.dispatched_dps.get(dpid_trigger)
                    data = {"dp": dpid_trigger, "value": dpid_value}
                    fire_event(event, data)

    @callback
    def status_updated(self, status: dict):
        """Device updated status."""
        if self._fake_gateway:
            # Fake gateways are only used to pass commands no need to update status.
            return
        self._handle_event(self._status, status)
        self._status.update(status)
        self._dispatch_status()

    @callback
    def disconnected(self):
        """Device disconnected."""

        def shutdown_entities(now=None):
            """Shutdown device entities"""
            if not self.connected:
                signal = f"localtuya_{self._device_config[CONF_DEVICE_ID]}"
                async_dispatcher_send(self._hass, signal, None)

        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        self._interface = None

        if self._sub_devices:
            for sub_dev in self._sub_devices.values():
                sub_dev.disconnected()

        if self._connect_task is not None:
            self._connect_task.cancel()
            self._connect_task = None

        # If it disconnects unexpectedly.
        if self._is_closing is not True and not self.is_subdevice:
            self.debug(f"Disconnected - waiting for discovery broadcast", force=True)
            # Try to quickly reconnect.
            self._is_closing = False
            self._config_entry.async_create_task(self._hass, self.async_connect())
        if not self._is_closing:
            async_call_later(self._hass, 5, shutdown_entities)


class LocalTuyaEntity(RestoreEntity, pytuya.ContextualLogger):
    """Representation of a Tuya entity."""

    _attr_device_class = None
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, device: TuyaDevice, device_config: dict, dp_id: str, logger, **kwargs
    ):
        """Initialize the Tuya entity."""
        super().__init__()
        self._device = device
        self._device_config = device_config
        self._config = get_entity_config(device_config, dp_id)
        self._dp_id = dp_id
        self._status = {}
        self._state = None
        self._last_state = None

        # Default value is available to be provided by Platform entities if required
        self._default_value = self._config.get(CONF_DEFAULT_VALUE)

        """ Restore on connect setting is available to be provided by Platform entities
        if required"""
        self.set_logger(logger, self._device_config[CONF_DEVICE_ID])
        self.debug(f"Initialized {self._config.get(CONF_PLATFORM)} [{self.name}]")

    async def async_added_to_hass(self):
        """Subscribe localtuya events."""
        await super().async_added_to_hass()

        self.debug(f"Adding {self.entity_id} with configuration: {self._config}")

        state = await self.async_get_last_state()
        if state:
            self.status_restored(state)

        def _update_handler(status):
            """Update entity state when status was updated."""
            if status is None:
                status = {}
            if self._status != status:
                self._status = status.copy()
                if status:
                    self.status_updated()

                # Update HA
                self.schedule_update_ha_state()

        signal = f"localtuya_{self._device_config[CONF_DEVICE_ID]}"

        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal, _update_handler)
        )

        signal = f"localtuya_entity_{self._device_config[CONF_DEVICE_ID]}"
        async_dispatcher_send(self.hass, signal, self.entity_id)

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes to be saved.

        These attributes are then available for restore when the
        entity is restored at startup.
        """
        attributes = {}
        if self._state is not None:
            attributes[ATTR_STATE] = self._state
        elif self._last_state is not None:
            attributes[ATTR_STATE] = self._last_state

        self.debug(f"Entity {self.name} - Additional attributes: {attributes}")
        return attributes

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the device registry."""
        model = self._device_config.get(CONF_MODEL, "Tuya generic")

        return DeviceInfo(
            # Serial numbers are unique identifiers within a specific domain
            identifiers={(DOMAIN, f"local_{self._device_config[CONF_DEVICE_ID]}")},
            name=self._device_config[CONF_FRIENDLY_NAME],
            manufacturer="Tuya",
            model=f"{model} ({self._device_config[CONF_DEVICE_ID]})",
            sw_version=self._device_config[CONF_PROTOCOL_VERSION],
        )

    @property
    def name(self) -> str:
        """Get name of Tuya entity."""
        return self._config.get(CONF_FRIENDLY_NAME)

    @property
    def icon(self) -> str | None:
        """Icon of the entity."""
        return self._config.get(CONF_ICON, None)

    @property
    def unique_id(self) -> str:
        """Return unique device identifier."""
        return f"local_{self._device_config[CONF_DEVICE_ID]}_{self._dp_id}"

    @property
    def available(self) -> bool:
        """Return if device is available or not."""
        return len(self._status) > 0

    @property
    def entity_category(self) -> str:
        """Return the category of the entity."""
        if category := self._config.get(CONF_ENTITY_CATEGORY):
            return EntityCategory(category) if category != "None" else None
        else:
            # Set Default values for unconfigured devices.
            if platform := self._config.get(CONF_PLATFORM):
                # Call default_category from config_flow  to set default values!
                # This will be removed after a while, this is only made to convert who came from main integration.
                # new users will be forced to choose category from config_flow.
                from .config_flow import default_category

                return default_category(platform)
        return None

    @property
    def device_class(self):
        """Return the class of this device."""
        return self._config.get(CONF_DEVICE_CLASS, self._attr_device_class)

    def has_config(self, attr) -> bool:
        """Return if a config parameter has a valid value."""
        value = self._config.get(attr, "-1")
        return value is not None and value != "-1"

    def dp_value(self, key):
        """Return cached value for DPS index or Entity Config Key."""
        requested_dp = str(key)
        # If requested_dp in DP ID, get cached value.
        if (value := self._status.get(requested_dp)) or value is not None:
            return value

        # If requested_dp is an config key get config dp then get cached value.
        if (conf_key := self._config.get(requested_dp)) or conf_key is not None:
            if (value := self._status.get(conf_key)) or value is not None:
                return value

        if value is None:
            self.debug(f"{self.name}: is requesting unknown DP Value {key}", force=True)

        return value

    def status_updated(self) -> None:
        """Device status was updated.

        Override in subclasses and update entity specific state.
        """
        state = self.dp_value(self._dp_id)
        self._state = state

        # Keep record in last_state as long as not during connection/re-connection,
        # as last state will be used to restore the previous state
        if (state is not None) and (not self._device.is_connecting):
            self._last_state = state

    def status_restored(self, stored_state) -> None:
        """Device status was restored.

        Override in subclasses and update entity specific state.
        """
        raw_state = stored_state.attributes.get(ATTR_STATE)
        if raw_state is not None:
            self._last_state = raw_state
            self.debug(
                f"Restoring state for entity: {self.name} - state: {str(self._last_state)}"
            )

    def default_value(self):
        """Return default value of this entity.

        Override in subclasses to specify the default value for the entity.
        """
        # Check if default value has been set - if not, default to the entity defaults.
        if self._default_value is None:
            self._default_value = self.entity_default_value()

        return self._default_value

    def entity_default_value(self):  # pylint: disable=no-self-use
        """Return default value of the entity type.

        Override in subclasses to specify the default value for the entity.
        """
        return 0

    def scale(self, value):
        """Return the scaled factor of the value, else same value."""
        scale_factor = self._config.get(CONF_SCALING)
        if scale_factor is not None and isinstance(value, (int, float)):
            value = round(value * scale_factor, 2)

        return value

    async def restore_state_when_connected(self) -> None:
        """Restore if restore_on_reconnect is set, or if no status has been yet found.

        Which indicates a DPS that needs to be set before it starts returning
        status.
        """
        restore_on_reconnect = self._config.get(CONF_RESTORE_ON_RECONNECT, False)
        passive_entity = self._config.get(CONF_PASSIVE_ENTITY, False)
        dp_id = str(self._dp_id)

        if not restore_on_reconnect and (dp_id in self._status or not passive_entity):
            self.debug(
                f"Entity {self.name} (DP {self._dp_id}) - Not restoring as restore on reconnect is "
                + "disabled for this entity and the entity has an initial status "
                + "or it is not a passive entity"
            )
            return

        self.debug(f"Attempting to restore state for entity: {self.name}")
        # Attempt to restore the current state - in case reset.
        restore_state = self._state

        # If no state stored in the entity currently, go from last saved state
        if (restore_state == STATE_UNKNOWN) | (restore_state is None):
            self.debug("No current state for entity")
            restore_state = self._last_state

        # If no current or saved state, then use the default value
        if restore_state is None:
            if passive_entity:
                self.debug("No last restored state - using default")
                restore_state = self.default_value()
            else:
                self.debug("Not a passive entity and no state found - aborting restore")
                return

        self.debug(
            f"Entity {self.name} (DP {self._dp_id}) - Restoring state: {str(restore_state)}"
        )

        # Manually initialise
        await self._device.set_dp(restore_state, self._dp_id)


class HassLocalTuyaData(NamedTuple):
    """LocalTuya data stored in homeassistant data object."""

    cloud_data: TuyaCloudApi
    devices: dict[str, TuyaDevice]
    unsub_listeners: list[CALLBACK_TYPE,]
