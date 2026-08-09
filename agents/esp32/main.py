# ProjectOS ESP32 accessory -- MicroPython.
#
# *"o esp32 tbm n precisa ser s'ó um sistema, poderia ser tbm algo q ajuda o rp,
# algum acessorio, diversas coisas da pra fazer com uma"*
#
# This is the accessory half. The board does not run ProjectOS; it pairs with
# one and becomes a pair of hands out in the house -- a temperature reading in
# the bird room, a button by the door, a relay on a lamp.
#
# Flash MicroPython, copy this file as main.py, edit the four settings below,
# and pair once from the Ajudantes screen.
#
# It speaks the same HTTP heartbeat a PC does. No websocket: an ESP32 that has
# to hold a socket open cannot deep-sleep, and a sensor that cannot sleep is a
# sensor with a cable running to it.

import json
import time

import machine
import network
import urequests

# --------------------------------------------------------------------------- settings

WIFI_SSID = "sua-rede"
WIFI_PASSWORD = "sua-senha"
SERVER = "http://projectos.local:8099"
# Leave empty on the first run and set PAIRING_CODE; the board prints the token
# it receives, and you paste it here so it survives a reboot.
TOKEN = ""
PAIRING_CODE = ""

NAME = "esp32 do quarto"
# Only claim what is actually wired up.
CAPABILITIES = ["sensor"]

# Pins. Set to None whatever this board does not have.
SENSOR_PIN = None       # e.g. 4 for a DS18B20 / DHT
BUTTON_PIN = None       # e.g. 0 for the BOOT button
RELAY_PIN = None        # e.g. 26

POLL_SECONDS = 15
RETRY_SECONDS = 10


# --------------------------------------------------------------------------- wifi


def connect_wifi():
    station = network.WLAN(network.STA_IF)
    station.active(True)
    if not station.isconnected():
        print("wifi: connecting to", WIFI_SSID)
        station.connect(WIFI_SSID, WIFI_PASSWORD)
        for _ in range(60):
            if station.isconnected():
                break
            time.sleep(0.5)
    if not station.isconnected():
        # Rebooting beats sitting there forever: the router may simply have been
        # slower to come back than the board was.
        print("wifi: no luck, rebooting")
        time.sleep(5)
        machine.reset()
    print("wifi:", station.ifconfig()[0])
    return station


# --------------------------------------------------------------------------- http


def post(path, payload):
    response = None
    try:
        response = urequests.post(
            SERVER + path, data=json.dumps(payload), headers={"Content-Type": "application/json"}
        )
        if response.status_code >= 400:
            print("server said", response.status_code, response.text[:120])
            return None
        return response.json()
    except Exception as exc:  # noqa: BLE001 - any failure here is "try again later"
        print("request failed:", exc)
        return None
    finally:
        if response is not None:
            # MicroPython does not close these for you, and a leaked socket on a
            # board with 4 sockets is a hang half an hour from now.
            try:
                response.close()
            except Exception:
                pass


def pair():
    print("pairing with code", PAIRING_CODE)
    answer = post("/api/helpers/pair", {
        "code": PAIRING_CODE,
        "name": NAME,
        "kind": "esp32",
        "platform": "micropython",
        "capabilities": CAPABILITIES,
        "facts": facts(),
    })
    if not answer:
        return ""
    print("paired. put this in TOKEN and reboot:")
    print(answer["token"])
    return answer["token"]


# --------------------------------------------------------------------------- hardware


def facts():
    return {
        "freq_mhz": machine.freq() // 1000000,
        "sensor_pin": SENSOR_PIN,
        "relay_pin": RELAY_PIN,
        "button_pin": BUTTON_PIN,
    }


def read_sensor():
    """Whatever is wired to SENSOR_PIN. Replace with your sensor's driver.

    The raw ADC reading is the honest default: it needs no library and it is
    obviously a placeholder, which is better than a temperature that is quietly
    wrong because the board is not the one this was written for.
    """
    if SENSOR_PIN is None:
        return None
    try:
        return machine.ADC(machine.Pin(SENSOR_PIN)).read_u16()
    except Exception as exc:  # noqa: BLE001
        print("sensor:", exc)
        return None


def set_relay(on):
    if RELAY_PIN is None:
        return False
    machine.Pin(RELAY_PIN, machine.Pin.OUT).value(1 if on else 0)
    return True


def run_job(job):
    kind = job.get("kind")
    payload = job.get("payload") or {}
    if kind == "ping":
        return {"pong": True}, ""
    if kind == "read":
        return {"value": read_sensor()}, ""
    if kind == "relay":
        if set_relay(bool(payload.get("on"))):
            return {"on": bool(payload.get("on"))}, ""
        return None, "esta placa nao tem rele configurado"
    return None, "esp32 nao sabe fazer " + str(kind)


# --------------------------------------------------------------------------- loop


def serve(token):
    button = None
    if BUTTON_PIN is not None:
        button = machine.Pin(BUTTON_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
    was_pressed = False

    while True:
        body = {"token": token, "capabilities": CAPABILITIES, "facts": facts()}
        reading = read_sensor()
        if reading is not None:
            body["facts"]["reading"] = reading
        if button is not None:
            pressed = button.value() == 0
            if pressed and not was_pressed:
                body["facts"]["button"] = "pressed"
            was_pressed = pressed

        answer = post("/api/helpers/agent/heartbeat", body)
        if answer is None:
            time.sleep(RETRY_SECONDS)
            continue

        job = answer.get("job")
        if job is None:
            time.sleep(answer.get("poll_seconds") or POLL_SECONDS)
            continue

        print("job:", job.get("kind"))
        try:
            result, error = run_job(job)
        except Exception as exc:  # noqa: BLE001
            result, error = None, str(exc)
        post(
            "/api/helpers/agent/jobs/" + job["id"] + "/done?token=" + token,
            {"result": result, "error": error},
        )


def main():
    connect_wifi()
    token = TOKEN
    if not token:
        if not PAIRING_CODE:
            print("set PAIRING_CODE (Ajudantes screen) and reboot")
            return
        token = pair()
        if not token:
            time.sleep(RETRY_SECONDS)
            machine.reset()
    serve(token)


main()
