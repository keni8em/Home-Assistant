import typing

from . import MerossApi, const as mlc
from .helpers.obfuscate import OBFUSCATE_DEVICE_ID_MAP, obfuscated_dict
from .meross_profile import MerossCloudProfile, MerossCloudProfileStore

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


async def async_get_device_diagnostics(
    hass, entry: "ConfigEntry", device
) -> typing.Mapping[str, typing.Any]:
    """Return diagnostics for a device entry."""
    return await async_get_config_entry_diagnostics(hass, entry)


async def async_get_config_entry_diagnostics(
    hass, entry: "ConfigEntry"
) -> typing.Mapping[str, typing.Any]:
    """Return diagnostics for a config entry."""
    unique_id = entry.unique_id
    assert unique_id, "unique_id must be set to a valid value"

    data = entry.data
    obfuscate = data.get(mlc.CONF_OBFUSCATE, True)
    data = obfuscated_dict(data) if obfuscate else dict(data)

    if unique_id == mlc.DOMAIN:
        # MQTT Hub entry
        return data

    unique_id = unique_id.split(".")
    if unique_id[0] == "profile":
        # profile entry
        profile_id = unique_id[1]
        if profile := MerossApi.profiles.get(profile_id):
            store_data = profile._data
        else:
            try:
                store_data = await MerossCloudProfileStore(profile_id).async_load()
            except Exception:
                store_data = None
        if obfuscate:
            store_data = obfuscated_dict(store_data or {})
            # the profile contains uuid as keys and obfuscation
            # is not smart enough (but OBFUSCATE_DEVICE_ID_MAP is already
            # filled with uuid(s) from the profile device_info(s) and
            # the device_info(s) were already obfuscated in data)
            store_data[MerossCloudProfile.KEY_DEVICE_INFO] = {
                OBFUSCATE_DEVICE_ID_MAP[device_id]: device_info
                for device_id, device_info in store_data[
                    MerossCloudProfile.KEY_DEVICE_INFO
                ].items()
            }
        data["store"] = store_data
        return data

    # device entry
    if device := MerossApi.devices.get(unique_id[0]):
        data["device"] = {
            "class": type(device).__name__,
            "conf_protocol": device.conf_protocol,
            "pref_protocol": device.pref_protocol,
            "curr_protocol": device.curr_protocol,
            "MQTT": {
                "cloud_profile": isinstance(device._profile, MerossCloudProfile),
                "locally_active": bool(device.mqtt_locallyactive),
                "mqtt_connection": bool(device._mqtt_connection),
                "mqtt_connected": bool(device._mqtt_connected),
                "mqtt_publish": bool(device._mqtt_publish),
                "mqtt_active": bool(device._mqtt_active),
            },
            "HTTP": {
                "http": bool(device._http),
                "http_active": bool(device._http_active),
            },
            "polling_period": device.polling_period,
            "polling_strategies": {
                strategy.namespace: strategy.lastrequest
                for strategy in device.polling_strategies.values()
            },
            "device_response_size_min": device.device_response_size_min,
            "device_response_size_max": device.device_response_size_max,
        }
        data["trace"] = await device.async_get_diagnostics_trace()
    return data