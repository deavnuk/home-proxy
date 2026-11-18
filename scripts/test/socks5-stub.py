import socket
import sys, struct, threading
def rx(s,n):
    b=b""
    while len(b)<n:
        d=s.recv(n-len(b))
        if not d: raise EOFError
        b+=d
    return b
def handle(c):
    try:
        h=rx(c,2); nm=h[1]; rx(c,nm); c.sendall(b"\x05\x00")
        ver,cmd,rsv,atyp=rx(c,4)
        if atyp==1: host=socket.inet_ntoa(rx(c,4))
        elif atyp==3: host=rx(c,rx(c,1)[0]).decode()
        elif atyp==4: host=socket.inet_ntop(socket.AF_INET6,rx(c,16))
        port=struct.unpack(">H",rx(c,2))[0]
        print(f"[stub] CONNECT {host}:{port}",flush=True)
        c.sendall(b"\x05\x00\x00\x01"+socket.inet_aton("0.0.0.0")+struct.pack(">H",0))
        req=c.recv(65536)
        print(f"[stub] got {len(req)} bytes: {req[:40]!r}",flush=True)
        body=b"PROXIED-VIA-STUB "+host.encode()
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"%len(body)+body)
        c.close()
    except Exception as e:
        print("[stub] err",repr(e),flush=True)
srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 11081
srv.bind(("0.0.0.0", PORT)); srv.listen(64)
print(f"[stub] listening 0.0.0.0:{PORT}",flush=True)
while True:
    c,_=srv.accept(); threading.Thread(target=handle,args=(c,),daemon=True).start()
