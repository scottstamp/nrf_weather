# Weather sources

Code: `host/wxd/weather.py`. Fixtures: `host/tests/fixtures/`. Verified live 2026-11-05.

## Location: A1K 1E1

- Geolocator (geo.ca) has the FSA `A1K` only (centroid 47.646, -52.724); full code A1K 1E1 is not geocodable offline/free. Zippopotam: A1K = **Torbay, NL**.
- So the address is in **Torbay** (north of St. John's, about 10 km from the airport), not Portugal Cove-St. Philip's.
- Used coordinates: **47.65, -52.73** (Open-Meteo snaps to 47.654, -52.747, 55 m elevation).
- ECCC has no citypage for Torbay. Nearest citypage sites in the collection: `nl-24` St. John's (47.56, -52.72, region "St. John's and vicinity") and `nl-43` Bay Roberts. **Use `nl-24`.** Its observation station is **St. John's Int'l Airport (CYYT, code `yyt`)**, 47.62N 52.75W.
- Expect Torbay to differ slightly from CYYT (coastal, a bit cooler/windier on a NE flow). Acceptable for a windowsill display.
- Timezone `America/St_Johns` (UTC-3:30 standard, -2:30 DST). Half-hour offset handled by `zoneinfo`.

## Primary: ECCC GeoMet OGC API, collection `citypageweather-realtime`

```
https://api.weather.gc.ca/collections/citypageweather-realtime/items/nl-24?f=json&lang=en
```

- One JSON (about 36 KB) has current conditions, 13 forecast periods, hourly forecast, warnings, sunrise/sunset. A GeoJSON Feature; `geometry` is the site point.
- Every value is wrapped `{"en": v, "fr": v}` (even with `lang=en`); the parser unwraps `en`.
- No key, no auth. Why chosen: single request, official (includes the warnings block), stable OGC API, same data as the weather.gc.ca page. The Datamart is the original publisher, but its layout changed in 2025 (the old `citypage_weather/xml/NL/s0000280_e.xml` path now 404s; dated dirs like `https://dd.weather.gc.ca/today/citypage_weather/NL/<HH>/...` exist), so the API is the safer bet.
- Other collections checked: `weather-alerts` (CAP-derived polygons, `alert_type`, `feature_name_en`, `status_en`; empty for bbox around St. John's when no alert) and `swob-realtime` (raw station obs). Not needed: the citypage `warnings` block already carries alerts for the site. Upgrade path if warnings prove incomplete: query `weather-alerts?bbox=...` and filter `status_en != "ended"`.
- Datamart citypage XML is a possible third fallback (not implemented).

### Field mapping (ECCC `properties.*` to model)

| Model | ECCC |
|---|---|
| `Current.temp_c` | `currentConditions.temperature.value.en` |
| `condition` | `currentConditions.condition.en` ("Mainly Clear") |
| `icon` | `currentConditions.iconCode.value` (see icon table); falls back to hourly[0], then forecast[0]. Day/night variant is then forced from obs time vs `riseSet` (obs icon can lag sunrise). |
| `feels_like_c` | `windChill.value`, else `humidex.value`; None when absent |
| `wind_kph`, `wind_gust_kph`, `wind_dir` | `wind.speed/gust/direction.value` (gust absent when calm) |
| `humidity_pct`, `pressure_kpa`, `pressure_tendency` | `relativeHumidity`, `pressure.value`, `pressure.tendency` (rising/falling/steady) |
| `observed_at`, `station` | `timestamp.en` (UTC), `station.value.en` |
| `Period.name` | `forecastGroup.forecasts[].period.textForecastName` ("Tonight", "Friday night") |
| `short_name` | derived: DAY, NIGHT, AFTN, EVE, FRI, FRI NT |
| `Period.icon` | `abbreviatedForecast.icon.value` |
| `temp_c`, `temp_is_high` | `temperatures.temperature[0].value` / `.class` (high/low) |
| `pop_pct` | `abbreviatedForecast.pop.value` if present, else max "NN percent chance" parsed from `textSummary` |
| `summary` | `abbreviatedForecast.textSummary` (+ "Amount X.") |
| `precip_amount` | `precipitation.accumulation.amount` (value + units) |
| `today_high_c/low_c` | first high / low among the first two periods |
| `sunrise/sunset` | `riseSet.sunrise/sunset.en` (UTC) |
| `Alert` | `warnings[]`: `type` (warning/watch/advisory/statement; `ended` dropped), `description` ("ORANGE WARNING - WIND" becomes "Wind warning"), `eventIssue`, `expiryTime` |

Alerts are sorted warning, watch, advisory, statement; ties by issue time. `warnings` is `[]` when none.

### ECCC icon codes (as mapped)

0-1 sunny: CLEAR_DAY. 2-5, 21-22: PARTLY_CLOUDY_DAY. 6: SHOWERS_DAY. 7, 15, 27, 37: SLEET. 8, 26, 38: FLURRIES. 9, 19, 39, 46-48: THUNDERSTORM. 10, 20: CLOUDY. 11-13: RAIN. 14: FREEZING_RAIN. 16-18: SNOW. 23, 24, 44: FOG. 25, 40: BLOWING_SNOW. 28: DRIZZLE. 30-31: CLEAR_NIGHT. 32-35: PARTLY_CLOUDY_NIGHT. 36: SHOWERS_NIGHT. 41-43, 45: WIND. 29 and unknown: UNKNOWN. (Code meanings are from ECCC's icon set; verify odd codes on first sighting.)

## Fallback: Open-Meteo

```
https://api.open-meteo.com/v1/forecast?latitude=47.6500&longitude=-52.7300&current=temperature_2m,relative_humidity_2m,apparent_temperature,is_day,weather_code,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,snowfall_sum,sunrise,sunset&models=gem_seamless&wind_speed_unit=kmh&timezone=America%2FSt_Johns&forecast_days=5
```

- `gem_seamless` = ECCC's GEM model blend, so numbers agree with the primary. Verified it returns data for Torbay.
- Mapping: `apparent_temperature` to feels-like; `pressure_msl` hPa / 10 to kPa; wind degrees to 16-point compass; `is_day` plus WMO `weather_code` to Icon (day/night aware); no pressure tendency, no station, no alerts (`[]`).
- Periods are synthesized: each day gives a "high" half (daily max) and a "night" half (daily min). Today's day half is dropped after sunset. Both halves of a day share the daily icon (night variant for the night half) and POP.
- Snow at WMO 73/75/86 with gusts >= 60 km/h becomes BLOWING_SNOW.

## Refresh cadence (matters: display refreshes only on content change)

| Data | Source cadence | Poll advice |
|---|---|---|
| ECCC current obs | hourly METAR/SYNOP-based obs at about :00 (CYYT also special obs); `lastUpdated` moves a few minutes after the hour (seen obs 10:00Z, file 10:03Z) | poll every 10-15 min; displayed values change at most hourly, plus `observed_at` |
| ECCC forecast | regular issues about 4x/day (roughly early morning, late morning, afternoon, evening local); unscheduled updates when conditions change | `forecastGroup.timestamp` shows issue time |
| ECCC warnings | event driven, minutes | same poll |
| Open-Meteo current | 15-min steps; model data updates a few times/day | do not use `fetched_at` or minute-level values in the image |

Implication: the image should show the observation hour, not fetch time (decision D5). Rounding temps to integers in the renderer hides most sub-hour noise. Open-Meteo `current` changes every 15 min, so when on fallback expect more refreshes; the daemon may want to hash only rounded values.

## Licensing and attribution

- ECCC data (GeoMet, Datamart): Open Government Licence - Canada / ECCC data servers end-use licence. Free reuse including commercial; attribution recommended: "Contains information licensed under the Open Government Licence - Canada; source: Environment and Climate Change Canada". Do not imply ECCC endorsement. Put it in the README.
- Open-Meteo: free for non-commercial use, no key; data CC BY 4.0, attribute "Weather data by Open-Meteo.com". Limit about 10,000 calls/day, 5,000/hour, 600/min; this daemon makes about 144/day.
- ECCC publishes no hard rate limit for api.weather.gc.ca but asks for reasonable use; polling every 10 min is fine. No account.
- User agent sent: `wxd/0.1 (e-paper weather display)`. No contact address included anywhere.

## Failure behavior

`fetch_weather`: ECCC (1 retry, 15 s timeout) then Open-Meteo; `RuntimeError` only if both fail. If ECCC works but has no current temperature (station obs gap), the current block is borrowed from Open-Meteo while periods/alerts stay ECCC. Responses with every field missing yield a model with None fields and `Icon.UNKNOWN`; ECCC and Open-Meteo responses with no temperature and no periods are treated as failures.

## Test fixtures

- `eccc_nl-24.json`: real capture 2026-11-05, trimmed to 6 periods and 2 hourly entries.
- `eccc_nl-24_warnings_synthetic.json`: same capture with a hand-built `warnings` block (real structure copied from live `qc-50`/`qc-d2` entries; NL had no active alert), plus removed `windChill`/`iconCode`.
- `open_meteo.json`: real capture, 5 days.
