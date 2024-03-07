"""Platform to locally control Tuya-based light devices."""
import logging
import textwrap
from functools import partial
from .config_flow import _col_to_select
from homeassistant.helpers import selector

import homeassistant.util.color as color_util
import voluptuous as vol
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    DOMAIN,
    LightEntityFeature,
    ColorMode,
    LightEntity,
)
from homeassistant.const import CONF_BRIGHTNESS, CONF_COLOR_TEMP, CONF_SCENE

from .common import LocalTuyaEntity, async_setup_entry
from .const import (
    CONF_BRIGHTNESS_LOWER,
    CONF_BRIGHTNESS_UPPER,
    CONF_COLOR,
    CONF_COLOR_MODE,
    CONF_COLOR_TEMP_MAX_KELVIN,
    CONF_COLOR_TEMP_MIN_KELVIN,
    CONF_COLOR_TEMP_REVERSE,
    CONF_SCENE_VALUES,
    CONF_MUSIC_MODE,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_MIN_KELVIN = 2700  # MIRED 370
DEFAULT_MAX_KELVIN = 6500  # MIRED 153

DEFAULT_COLOR_TEMP_REVERSE = False

DEFAULT_LOWER_BRIGHTNESS = 29
DEFAULT_UPPER_BRIGHTNESS = 1000

MODE_COLOR = "colour"
MODE_MUSIC = "music"
MODE_SCENE = "scene"
MODE_WHITE = "white"

SCENE_CUSTOM = "Custom"
SCENE_MUSIC = "Music"

SCENE_LIST_RGBW_1000 = {
    "Night": "000e0d0000000000000000c80000",
    "Read": "010e0d0000000000000003e801f4",
    "Meeting": "020e0d0000000000000003e803e8",
    "Leasure": "030e0d0000000000000001f401f4",
    "Soft": "04464602007803e803e800000000464602007803e8000a00000000",
    "Rainbow": "05464601000003e803e800000000464601007803e803e80000000046460100f003e803"
    + "e800000000",
    "Shine": "06464601000003e803e800000000464601007803e803e80000000046460100f003e803e8"
    + "00000000",
    "Beautiful": "07464602000003e803e800000000464602007803e803e80000000046460200f003e8"
    + "03e800000000464602003d03e803e80000000046460200ae03e803e800000000464602011303e80"
    + "3e800000000",
}

SCENE_LIST_RGBW_255 = {
    "Night": "bd76000168ffff",
    "Read": "fffcf70168ffff",
    "Meeting": "cf38000168ffff",
    "Leasure": "3855b40168ffff",
    "Scenario 1": "scene_1",
    "Scenario 2": "scene_2",
    "Scenario 3": "scene_3",
    "Scenario 4": "scene_4",
}

SCENE_LIST_RGB_1000 = {
    "Night": "000e0d00002e03e802cc00000000",
    "Read": "010e0d000084000003e800000000",
    "Working": "020e0d00001403e803e800000000",
    "Leisure": "030e0d0000e80383031c00000000",
    "Soft": "04464602007803e803e800000000464602007803e8000a00000000",
    "Colorful": "05464601000003e803e800000000464601007803e803e80000000046460100f003e80"
    + "3e800000000464601003d03e803e80000000046460100ae03e803e800000000464601011303e803"
    + "e800000000",
    "Dazzling": "06464601000003e803e800000000464601007803e803e80000000046460100f003e80"
    + "3e800000000",
    "Music": "07464602000003e803e800000000464602007803e803e80000000046460200f003e803e8"
    + "00000000464602003d03e803e80000000046460200ae03e803e800000000464602011303e803e80"
    + "0000000",
}


def map_range(value, from_lower, from_upper, to_lower=0, to_upper=255, reverse=False):
    """Map a value in one range to another."""
    if reverse:
        value = from_upper - value + from_lower
    mapped = (value - from_lower) * (to_upper - to_lower) / (
        from_upper - from_lower
    ) + to_lower
    return round(min(max(mapped, to_lower), to_upper))


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_BRIGHTNESS): _col_to_select(dps, is_dps=True),
        vol.Optional(CONF_COLOR_TEMP): _col_to_select(dps, is_dps=True),
        vol.Optional(CONF_BRIGHTNESS_LOWER, default=DEFAULT_LOWER_BRIGHTNESS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=10000)
        ),
        vol.Optional(CONF_BRIGHTNESS_UPPER, default=DEFAULT_UPPER_BRIGHTNESS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=10000)
        ),
        vol.Optional(CONF_COLOR_MODE): _col_to_select(dps, is_dps=True),
        vol.Optional(CONF_COLOR): _col_to_select(dps, is_dps=True),
        vol.Optional(CONF_COLOR_TEMP_MIN_KELVIN, default=DEFAULT_MIN_KELVIN): vol.All(
            vol.Coerce(int), vol.Range(min=1500, max=8000)
        ),
        vol.Optional(CONF_COLOR_TEMP_MAX_KELVIN, default=DEFAULT_MAX_KELVIN): vol.All(
            vol.Coerce(int), vol.Range(min=1500, max=8000)
        ),
        vol.Optional(CONF_COLOR_TEMP_REVERSE, default=DEFAULT_COLOR_TEMP_REVERSE): bool,
        vol.Optional(CONF_SCENE): _col_to_select(dps, is_dps=True),
        vol.Optional(CONF_SCENE_VALUES): selector.ObjectSelector(),
        vol.Optional(CONF_MUSIC_MODE, default=False): bool,
    }


class LocaltuyaLight(LocalTuyaEntity, LightEntity):
    """Representation of a Tuya light."""

    def __init__(
        self,
        device,
        config_entry,
        lightid,
        **kwargs,
    ):
        """Initialize the Tuya light."""
        super().__init__(device, config_entry, lightid, _LOGGER, **kwargs)
        self._state = False
        self._brightness = None
        self._color_temp = None
        self._lower_brightness = int(
            self._config.get(CONF_BRIGHTNESS_LOWER, DEFAULT_LOWER_BRIGHTNESS)
        )
        self._upper_brightness = int(
            self._config.get(CONF_BRIGHTNESS_UPPER, DEFAULT_UPPER_BRIGHTNESS)
        )
        self._upper_color_temp = self._upper_brightness
        self._min_kelvin = int(
            self._config.get(CONF_COLOR_TEMP_MIN_KELVIN, DEFAULT_MIN_KELVIN)
        )
        self._max_kelvin = int(
            self._config.get(CONF_COLOR_TEMP_MAX_KELVIN, DEFAULT_MAX_KELVIN)
        )
        self._color_temp_reverse = self._config.get(
            CONF_COLOR_TEMP_REVERSE, DEFAULT_COLOR_TEMP_REVERSE
        )
        self._hs = None
        self._effect = None
        self._effect_list = []
        self._scenes = None
        if self.has_config(CONF_SCENE):
            if self.has_config(CONF_SCENE_VALUES):
                values_list = list(self._config.get(CONF_SCENE_VALUES))
                values_name = list(self._config.get(CONF_SCENE_VALUES).values())
                self._scenes = dict(zip(values_name, values_list))
            elif int(self._config.get(CONF_SCENE)) < 20:
                self._scenes = SCENE_LIST_RGBW_255
            elif self._config.get(CONF_BRIGHTNESS) is None:
                self._scenes = SCENE_LIST_RGB_1000
            else:
                self._scenes = SCENE_LIST_RGBW_1000
            self._effect_list = list(self._scenes.keys())
        if self._config.get(CONF_MUSIC_MODE):
            self._effect_list.append(SCENE_MUSIC)

    @property
    def is_on(self):
        """Check if Tuya light is on."""
        return self._state

    @property
    def brightness(self):
        """Return the brightness of the light."""
        if self.is_color_mode or self.is_white_mode:
            return map_range(
                self._brightness, self._lower_brightness, self._upper_brightness, 0, 255
            )
        return None

    @property
    def hs_color(self):
        """Return the hs color value."""
        if self.is_color_mode:
            return self._hs
        if (
            ColorMode.HS in self.supported_color_modes
            and not ColorMode.COLOR_TEMP in self.supported_color_modes
        ):
            return [0, 0]
        return None

    @property
    def color_temp(self):
        """Return the color_temp of the light."""
        if self._color_temp is None:
            return
        if self.has_config(CONF_COLOR_TEMP):
            color_temp = (
                self._upper_color_temp - self._color_temp
                if self._color_temp_reverse
                else self._color_temp
            )
            return int(
                self.max_mireds
                - (
                    ((self.max_mireds - self.min_mireds) / self._upper_color_temp)
                    * color_temp
                )
            )
        return None

    @property
    def min_mireds(self):
        """Return color temperature min mireds."""
        return color_util.color_temperature_kelvin_to_mired(self._max_kelvin)

    @property
    def max_mireds(self):
        """Return color temperature max mireds."""
        return color_util.color_temperature_kelvin_to_mired(self._min_kelvin)

    @property
    def effect(self):
        """Return the current effect for this light."""
        if self.is_scene_mode or self.is_music_mode:
            return self._effect
        return None

    @property
    def effect_list(self):
        """Return the list of supported effects for this light."""
        if len(self._effect_list) > 0:
            return self._effect_list
        return None

    @property
    def supported_color_modes(self) -> set[ColorMode] | set[str] | None:
        """Flag supported color modes."""
        color_modes: set[ColorMode] = set()

        if self.has_config(CONF_COLOR_TEMP):
            color_modes.add(ColorMode.COLOR_TEMP)
        if self.has_config(CONF_COLOR):
            color_modes.update({ColorMode.HS, ColorMode.BRIGHTNESS})
        if self.has_config(CONF_BRIGHTNESS):
            color_modes.add(ColorMode.BRIGHTNESS)
        return color_modes

    @property
    def supported_features(self):
        """Flag supported features."""
        supports = 0
        if self.has_config(CONF_SCENE) or self.has_config(CONF_MUSIC_MODE):
            supports |= LightEntityFeature.EFFECT
        return supports

    @property
    def is_white_mode(self):
        """Return true if the light is in white mode."""
        color_mode = self.__get_color_mode()
        return color_mode is None or color_mode == MODE_WHITE

    @property
    def is_color_mode(self):
        """Return true if the light is in color mode."""
        color_mode = self.__get_color_mode()
        return color_mode is not None and color_mode == MODE_COLOR

    @property
    def is_scene_mode(self):
        """Return true if the light is in scene mode."""
        color_mode = self.__get_color_mode()
        return color_mode is not None and color_mode.startswith(MODE_SCENE)

    @property
    def is_music_mode(self):
        """Return true if the light is in music mode."""
        color_mode = self.__get_color_mode()
        return color_mode is not None and color_mode == MODE_MUSIC

    def __is_color_rgb_encoded(self):
        return len(self.dp_value(CONF_COLOR)) > 12

    def __find_scene_by_scene_data(self, data):
        return next(
            (item for item in self._effect_list if self._scenes.get(item) == data),
            SCENE_CUSTOM,
        )

    def __get_color_mode(self):
        return (
            self.dp_value(CONF_COLOR_MODE)
            if self.has_config(CONF_COLOR_MODE)
            else MODE_WHITE
        )

    async def async_turn_on(self, **kwargs):
        """Turn on or control the light."""
        states = {}
        if not self.is_on:
            states[self._dp_id] = True
        features = self.supported_features
        color_modes = self.supported_color_modes
        brightness = None
        if ATTR_EFFECT in kwargs and (features & LightEntityFeature.EFFECT):
            scene = self._scenes.get(kwargs[ATTR_EFFECT])
            if scene is not None:
                if scene.startswith(MODE_SCENE):
                    states[self._config.get(CONF_COLOR_MODE)] = scene
                else:
                    states[self._config.get(CONF_COLOR_MODE)] = MODE_SCENE
                    states[self._config.get(CONF_SCENE)] = scene
            elif kwargs[ATTR_EFFECT] == SCENE_MUSIC:
                states[self._config.get(CONF_COLOR_MODE)] = MODE_MUSIC

        if ATTR_BRIGHTNESS in kwargs and ColorMode.BRIGHTNESS in color_modes:
            brightness = map_range(
                int(kwargs[ATTR_BRIGHTNESS]),
                0,
                255,
                self._lower_brightness,
                self._upper_brightness,
            )
            if self.is_white_mode or self.dp_value(CONF_COLOR) is None:
                states[self._config.get(CONF_BRIGHTNESS)] = brightness
            else:
                if self.__is_color_rgb_encoded():
                    rgb = color_util.color_hsv_to_RGB(
                        self._hs[0],
                        self._hs[1],
                        int(brightness * 100 / self._upper_brightness),
                    )
                    color = "{:02x}{:02x}{:02x}{:04x}{:02x}{:02x}".format(
                        round(rgb[0]),
                        round(rgb[1]),
                        round(rgb[2]),
                        round(self._hs[0]),
                        round(self._hs[1] * 255 / 100),
                        brightness,
                    )
                else:
                    color = "{:04x}{:04x}{:04x}".format(
                        round(self._hs[0]), round(self._hs[1] * 10.0), brightness
                    )
                states[self._config.get(CONF_COLOR)] = color
                states[self._config.get(CONF_COLOR_MODE)] = MODE_COLOR

        if ATTR_HS_COLOR in kwargs and ColorMode.HS in color_modes:
            if brightness is None:
                brightness = self._brightness
            hs = kwargs[ATTR_HS_COLOR]
            if hs[1] == 0 and self.has_config(CONF_BRIGHTNESS):
                states[self._config.get(CONF_BRIGHTNESS)] = brightness
                states[self._config.get(CONF_COLOR_MODE)] = MODE_WHITE
            else:
                if self.__is_color_rgb_encoded():
                    rgb = color_util.color_hsv_to_RGB(
                        hs[0], hs[1], int(brightness * 100 / self._upper_brightness)
                    )
                    color = "{:02x}{:02x}{:02x}{:04x}{:02x}{:02x}".format(
                        round(rgb[0]),
                        round(rgb[1]),
                        round(rgb[2]),
                        round(hs[0]),
                        round(hs[1] * 255 / 100),
                        brightness,
                    )
                else:
                    color = "{:04x}{:04x}{:04x}".format(
                        round(hs[0]), round(hs[1] * 10.0), brightness
                    )
                states[self._config.get(CONF_COLOR)] = color
                states[self._config.get(CONF_COLOR_MODE)] = MODE_COLOR

        if ATTR_COLOR_TEMP in kwargs and ColorMode.COLOR_TEMP in color_modes:
            if brightness is None:
                brightness = self._brightness
            mired = int(kwargs[ATTR_COLOR_TEMP])
            if self._color_temp_reverse:
                mired = self.max_mireds - (mired - self.min_mireds)
            if mired < self.min_mireds:
                mired = self.min_mireds
            elif mired > self.max_mireds:
                mired = self.max_mireds
            color_temp = int(
                self._upper_color_temp
                - (self._upper_color_temp / (self.max_mireds - self.min_mireds))
                * (mired - self.min_mireds)
            )
            states[self._config.get(CONF_COLOR_MODE)] = MODE_WHITE
            states[self._config.get(CONF_BRIGHTNESS)] = brightness
            states[self._config.get(CONF_COLOR_TEMP)] = color_temp

        await self._device.set_dps(states)

    async def async_turn_off(self, **kwargs):
        """Turn Tuya light off."""
        await self._device.set_dp(False, self._dp_id)

    def status_updated(self):
        """Device status was updated."""
        self._state = self.dp_value(self._dp_id)
        supported = self.supported_features
        self._effect = None
        if ColorMode.BRIGHTNESS in self.supported_color_modes:
            self._brightness = self.dp_value(CONF_BRIGHTNESS)

        if ColorMode.HS in self.supported_color_modes:
            color = self.dp_value(CONF_COLOR)
            if color is not None and not self.is_white_mode:
                if self.__is_color_rgb_encoded():
                    hue = int(color[6:10], 16)
                    sat = int(color[10:12], 16)
                    value = int(color[12:14], 16)
                    self._hs = [hue, (sat * 100 / 255)]
                    self._brightness = value
                else:
                    hue, sat, value = [
                        int(value, 16) for value in textwrap.wrap(color, 4)
                    ]
                    self._hs = [hue, sat / 10.0]
                    self._brightness = value
            elif self._brightness is None:
                self._brightness = 20

        if ColorMode.COLOR_TEMP in self.supported_color_modes:
            self._color_temp = self.dp_value(CONF_COLOR_TEMP)

        if self.is_scene_mode and supported & LightEntityFeature.EFFECT:
            if self.dp_value(CONF_COLOR_MODE) != MODE_SCENE:
                self._effect = self.__find_scene_by_scene_data(
                    self.dp_value(CONF_COLOR_MODE)
                )
            else:
                self._effect = self.__find_scene_by_scene_data(
                    self.dp_value(CONF_SCENE)
                )
                if self._effect == SCENE_CUSTOM:
                    if SCENE_CUSTOM not in self._effect_list:
                        self._effect_list.append(SCENE_CUSTOM)
                elif SCENE_CUSTOM in self._effect_list:
                    self._effect_list.remove(SCENE_CUSTOM)

        if self.is_music_mode and supported & LightEntityFeature.EFFECT:
            self._effect = SCENE_MUSIC


async_setup_entry = partial(async_setup_entry, DOMAIN, LocaltuyaLight, flow_schema)
