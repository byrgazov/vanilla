import vanilla

hub    = vanilla.Hub()
server = hub.tcp.listen()

print('Listening on port: {0}'.format(server.port))

try:
    for no, conn in enumerate(server, 1):
        conn.send('Hi ({})\n'.format(no).encode())
        conn.close()
except KeyboardInterrupt:
    print('[KeyboardInterrupt]')
