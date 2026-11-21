"""
Fake A7672S + GSM Bridge, for trying the app without hardware.

    python tools/fake_modem.py
then in the app Settings choose Wi-Fi bridge, IP 127.0.0.1 (or Auto finds it).

Type in this console:  sms <number> <text>   -> simulate incoming SMS
                       ring <number>          -> simulate incoming call
"""
import asyncio
import json
import sys

SIM = {1: ("REC READ", "+919800000001", "24/11/20,09:15:00+22", "Hello from the fake modem")}
writer_ref = {"w": None}
FILES = {"Vaanima_Vaasadi_Vaagi.amr": 19206}


async def send(w, *lines):
    for l in lines:
        w.write(f"\r\n{l}\r\n".encode())
    await w.drain()


async def handle(reader, writer):
    writer_ref["w"] = writer
    print("app connected")
    buf = b""
    cmgs = None
    try:
        while True:
            data = await reader.read(1024)
            if not data:
                break
            buf += data
            if cmgs is not None:
                if b"\x1a" in buf:
                    body, buf = buf.split(b"\x1a", 1)
                    print(f"SMS to {cmgs}: {body.decode(errors='replace')}")
                    await asyncio.sleep(0.8)
                    await send(writer, "+CMGS: 12", "OK")
                    cmgs = None
                continue
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                c = line.decode().strip()
                if not c:
                    continue
                u = c.upper()
                if u.startswith("AT+CMGS="):
                    cmgs = c.split("=", 1)[1]
                    writer.write(b"\r\n> ")
                    await writer.drain()
                    break
                r = {"AT+CSQ": ["+CSQ: 21,99"], "AT+CPIN?": ["+CPIN: READY"],
                     "AT+CEREG?": ["+CEREG: 0,1"], "AT+CREG?": ["+CREG: 2,1"],
                     'AT+COPS?': ['+COPS: 0,0,"FakeTel 4G",7'], "AT+CGMM": ["A7672S"],
                     "AT+CGSN": ["860000000000001"], "AT+CNUM": ['+CNUM: "","+919999999999",145']}.get(u)
                if u.startswith("AT+CFTRANRX="):
                    name, size = c.split("=",1)[1].rsplit(",",1)
                    writer.write(b"\r\n>"); await writer.drain()
                    need = int(size); got = b""
                    while len(got) < need:
                        got += await reader.read(65536)
                    FILES[name.strip('"').replace("c:/","")] = len(got)
                    await send(writer, "OK"); continue
                if u == "AT+FSLS":
                    r = ["+FSLS: FILES:"] + list(FILES)
                elif u.startswith("AT+FSATTRI="):
                    r = [f"+FSATTRI: {FILES.get(c.split('=',1)[1].strip(chr(34)), 0)}"]
                elif u == "AT+FSMEM":
                    r = [f"+FSMEM: C:(720896,{340000 + sum(FILES.values())})"]
                elif u.startswith("AT+FSDEL="):
                    FILES.pop(c.split("=",1)[1].strip('"'), None)
                elif u.startswith("AT+CCMXPLAY="):
                    await send(writer, "OK", "+CCMXPLAY: 1")
                    asyncio.create_task(fake_keys(writer)); continue
                elif u == 'AT+CMGL="ALL"':
                    r = []
                    for i, (st, n, t, txt) in SIM.items():
                        r += [f'+CMGL: {i},"{st}","{n}","","{t}",145,{len(txt)}', txt]
                elif u.startswith("AT+CMGR="):
                    i = int(u.split("=")[1])
                    st, n, t, txt = SIM.get(i, ("", "", "", ""))
                    r = [f'+CMGR: "{st}","{n}","","{t}",145,4,0,0,"+919000000000",145,{len(txt)}', txt]
                elif u.startswith("ATD"):
                    num = c[3:].rstrip(";")
                    await send(writer, "OK", f'+CLCC: 1,0,2,0,0,"{num}",129')
                    asyncio.create_task(fake_answer(writer, num))
                    continue
                elif u in ("AT+CHUP", "ATH"):
                    await send(writer, "OK", "VOICE CALL: END: 000012")
                    continue
                elif u == "ATA":
                    await send(writer, "OK", "VOICE CALL: BEGIN")
                    continue
                await send(writer, *(r or []), "OK")
    finally:
        print("app disconnected")
        writer_ref["w"] = None


async def fake_keys(w):
    await asyncio.sleep(1.5)
    await send(w, "+RXDTMF: 1")


async def fake_answer(w, num):
    await asyncio.sleep(1.5)
    await send(w, f'+CLCC: 1,0,3,0,0,"{num}",129')
    await asyncio.sleep(2)
    await send(w, "VOICE CALL: BEGIN", f'+CLCC: 1,0,0,0,0,"{num}",129')


class Disc(asyncio.DatagramProtocol):
    def connection_made(self, t):
        self.t = t

    def datagram_received(self, data, addr):
        if data.startswith(b"GSM_BRIDGE_DISCOVER"):
            self.t.sendto(json.dumps({"name": "fake-bridge", "ip": "127.0.0.1", "port": 7000,
                                      "modem": True}).encode(), addr)


async def console():
    loop = asyncio.get_running_loop()
    idx = 10
    while True:
        line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
        w = writer_ref["w"]
        if not w or not line:
            continue
        cmd, _, rest = line.partition(" ")
        if cmd == "sms":
            num, _, txt = rest.partition(" ")
            idx += 1
            SIM[idx] = ("REC UNREAD", num, "24/11/21,12:00:00+22", txt)
            await send(w, f'+CMTI: "SM",{idx}')
        elif cmd == "ring":
            await send(w, "RING", f'+CLIP: "{rest}",145,,,,0')


async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", 7000)
    await asyncio.get_running_loop().create_datagram_endpoint(Disc, local_addr=("0.0.0.0", 7002))
    print("fake modem on tcp 7000 / udp 7002.  Commands: sms <num> <text> | ring <num>")
    async with srv:
        await asyncio.gather(srv.serve_forever(), console())


if __name__ == "__main__":
    asyncio.run(main())
