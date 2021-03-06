import threading
from xmlrpc.server import SimpleXMLRPCServer

from commons.loggers import default_logger, request_logger
from commons.metadata_manager import load_metadata, update_metadata, OplogActions
from commons.settings import CHUNK_SIZE, DEFAULT_MASTER_PORT, DEFAULT_IP, OP_LOG_FILENAME
from master.chunk_manager import ChunkManager
from master.namespace_manager import NamespaceManager


class Master:
    __slots__ = 'my_addr', 'client_id', 'mutex', \
                'metadata_file', 'namespace_manager', 'chunk_manager'

    def __init__(self, my_addr, metadata_file):
        self.my_addr = my_addr
        self.client_id = 0  # counter to give next client ID
        self.mutex = threading.Lock()  # TODO: probably use a re entrant lock
        self.metadata_file = metadata_file  # File that contains masters metadata

        self.namespace_manager = NamespaceManager()
        self.chunk_manager = ChunkManager()

    def __repr__(self):
        return f"""Master(me={self.my_addr},
        client_id={self.client_id},
        namespace_manager={self.namespace_manager},
        chunk_manager={self.chunk_manager})"""

    def test_ok(self):
        """A quick test to see if server is working fine"""
        return "Ok"

    def unique_client_id(self):
        """
        When a client is attached to the master,
        it calls this function to get a unique client ID.
        """
        rlog.info("Granting client id=%d", self.client_id + 1)

        with self.mutex:
            self.client_id += 1

            # make client id persistent in metadata file
            update_metadata(self.metadata_file, OplogActions.GRANT_CLIENT_ID, self.client_id)

            return self.client_id

    def create(self, path):
        """
        Will be called by client to create a file in the namespace (in memory)
        :param path: name of the file
        :return: True if successful and errors if any.
        """
        rlog.info("args: path=%s", path)
        res, err = self.namespace_manager.create(path)

        if not err:
            # Log this operation to oplog
            update_metadata(self.metadata_file, OplogActions.CREATE_FILE, path)

        return res, err

    def add_chunk(self, path, chunk_index):
        """
        Client calls to get a new chunk.
        :param path: name of the file
        :param chunk_index:
        :return: chunk_handle of the chunk created and address of chunkservers containing that chunk
        """
        rlog.info("args: path=%s, chunk_index=%d", path, chunk_index)
        info, err = self.chunk_manager.add_chunk(path, chunk_index)
        if err:
            return None, None, err

        # Log this operation to oplog
        update_metadata(self.metadata_file, OplogActions.ADD_CHUNK,
                        (path, chunk_index, info.chunk_handle, info.chunk_locations, info.chunk_handle))

        return info.chunk_handle, info.chunk_locations, None

    def find_locations(self, path, chunk_index):
        """
        Returns CHUNK HANDLE and an array of CHUNK LOCATIONS for a given file name and chunk index.
        :param path:
        :param chunk_index:
        :return: chunk_info {chunk_handle, chunk_locations} and errors.
        """
        rlog.info("args: path=%s, chunk_index=%d", path, chunk_index)
        chunk_locations, chunk_handle, err = self.chunk_manager.find_locations(path, chunk_index)

        return chunk_locations, chunk_handle, err

    def find_lease_holder(self, chunk_handle):
        """
        Client calls to get the PRIMARY chunk server for a given chunk handle.
        If there is no current lease holder, master will automatically select
        one of the replicas to be the primary, and grant lease to that chunk server.
        :param chunk_handle:
        :return: lease holder, lease expiration timestamp, errors
        """
        rlog.info("args: chunk_handle=%d", chunk_handle)
        lease, err = self.chunk_manager.find_lease_holder(chunk_handle)
        if err:
            return None, None, err

        return lease.primary, lease.expiration, None

    def report_chunk(self, server, chunk_handle, chunk_index, length, path):
        """
        Called by chunk servers to tell the master that they have a certain chunk and
        the number of defined bytes in that chunk.
        :param server:
        :param chunk_handle:
        :param chunk_index:
        :param length:
        :param path:
        :return:
        """
        rlog.debug(
            f"received request from chunk server:{server} with args: "
            f"chunk_handle={chunk_handle}, length={length}, chunk_index={chunk_index}, path={path}")

        # FIXME: path_index looks unnecessary :/
        path_index, err = self.chunk_manager.get_path_index_from_handle(chunk_handle)

        if err:
            return None, err

        assert path_index.index == chunk_index

        self.chunk_manager.set_chunk_location(chunk_handle, server)

        # Update file size information
        file_length, err = self.namespace_manager.get_file_length(path_index.path)

        new_length = CHUNK_SIZE * path_index.index + length

        log.debug(f"new calculated file size= {new_length},  "
                  f"old_file_size={file_length}, "
                  f"this chunk's size={length}, "
                  f"chunk index={path_index.index}")

        if new_length > file_length:
            self.namespace_manager.set_file_length(path_index.path, new_length)

        return None

    def create_dir(self, path):
        """Will be called by client to create a dir in the namespace"""
        rlog.info("CREATE DIR API called")
        res, err = self.namespace_manager.create_dir(path)
        if not err:
            update_metadata(self.metadata_file, OplogActions.CREATE_DIR, path)

        return res, err

    def list_allfiles(self, path):
        """Will be called by client to list all files present in given directory path"""
        rlog.info("LIST FILES API called")
        res, err = self.namespace_manager.list_allfiles(path)
        return res, err

    def delete(self, path):
        """Will be called by client to delete a specific file"""
        rlog.info("DELETE FILE API called")
        self.chunk_manager.update_deletechunk_list(path)
        err = self.namespace_manager.delete(path)

        if not err:
            update_metadata(self.metadata_file, OplogActions.DELETE_FILE, path)

        return err

    def get_file_length(self, path):
        """Will be called by client to get the file length"""
        rlog.info("GET FILE LENGTH API Called")
        file_length, err = self.namespace_manager.get_file_length(path)
        return file_length, err

    def notify_master(self, chunksrv_addr, chunk_handles):
        """
        When a chunk server is created,
        it calls this function to notify master of its presence.
        Then master adds this address to its chunkserver list
        :param chunk_handles: list of chunk handles at that chunk server
        :param chunksrv_addr: http://<ip_addr>:<port>
        :return: None
        """
        rlog.info("Received registration request from chunk server at: %s", chunksrv_addr)
        self.chunk_manager.update_chunkserver_list(chunksrv_addr)

        for chunk_handle in chunk_handles:
            self.chunk_manager.set_chunk_location(chunk_handle, chunksrv_addr)
        # Log this operation to oplog
        update_metadata(self.metadata_file, OplogActions.NOTIFY_MASTER, chunksrv_addr)

    def heartbeat(self):
        import threading
        bg_thread = threading.Thread(target=self.chunk_manager.beat, args=())
        # Run the thread in daemon mode.
        # This allows the main application to exit even though the thread is running.
        # It will also (therefore) make it possible to use ctrl+c to terminate the application.
        bg_thread.daemon = True
        bg_thread.start()


def start_master(ip, port):
    m = Master(f'http://{ip}:{port}', OP_LOG_FILENAME)

    # restore previous launch's meta data
    load_metadata(m)

    # one time polling to get the list of chunks from each chunk server
    m.chunk_manager.poll_chunkservers()

    # call heartbeat
    m.heartbeat()

    master_server = SimpleXMLRPCServer((ip, port),
                                       logRequests=True,
                                       allow_none=True)

    # Read: https://gist.github.com/abnvanand/199cacf6c8f45258ff096b842b77b216
    master_server.register_introspection_functions()

    # register all methods to be available to client
    # can either use register_function(<function's_name>)
    # or register_instance(<class's_instance>)  # All the methods of the instance are published as XML-RPC methods
    master_server.register_instance(m)

    print("Master running at: ", m.my_addr)

    master_server.serve_forever()

    # TODO: launch background tasks (eg. gc, heartbeat) in a separate thread


if __name__ == '__main__':
    log = default_logger
    rlog = request_logger

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--ip', default=DEFAULT_IP)
    parser.add_argument('--port', type=int, default=DEFAULT_MASTER_PORT)
    args = parser.parse_args()

    start_master(args.ip, args.port)
