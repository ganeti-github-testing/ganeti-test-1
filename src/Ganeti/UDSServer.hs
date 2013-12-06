{-# LANGUAGE TemplateHaskell #-}

{-| Implementation of the Ganeti Unix Domain Socket JSON server interface.

-}

{-

Copyright (C) 2013 Google Inc.

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
02110-1301, USA.

-}

module Ganeti.UDSServer
  ( Client
  , RecvResult(..)
  , MsgKeys(..)
  , strOfKey
  , getLuxiClient
  , getLuxiServer
  , acceptClient
  , closeClient
  , closeServer
  , buildResponse
  , recvMsg
  , recvMsgExt
  , sendMsg
  ) where

import Control.Exception (catch)
import Data.IORef
import qualified Data.ByteString as B
import qualified Data.ByteString.Lazy as BL
import qualified Data.ByteString.UTF8 as UTF8
import qualified Data.ByteString.Lazy.UTF8 as UTF8L
import Data.Word (Word8)
import Control.Monad
import qualified Network.Socket as S
import System.Directory (removeFile)
import System.IO (hClose, hFlush, hWaitForInput, Handle, IOMode(..))
import System.IO.Error (isEOFError)
import System.Timeout
import Text.JSON (encodeStrict)
import Text.JSON.Types

import Ganeti.Constants
import Ganeti.Runtime (GanetiDaemon(..), MiscGroup(..), GanetiGroup(..))
import Ganeti.THH
import Ganeti.Utils


-- * Utility functions

-- | Wrapper over System.Timeout.timeout that fails in the IO monad.
withTimeout :: Int -> String -> IO a -> IO a
withTimeout secs descr action = do
  result <- timeout (secs * 1000000) action
  case result of
    Nothing -> fail $ "Timeout in " ++ descr
    Just v -> return v


-- * Generic protocol functionality

-- | Result of receiving a message from the socket.
data RecvResult = RecvConnClosed    -- ^ Connection closed
                | RecvError String  -- ^ Any other error
                | RecvOk String     -- ^ Successfull receive
                  deriving (Show, Eq)


-- | The end-of-message separator.
eOM :: Word8
eOM = 3

-- | The end-of-message encoded as a ByteString.
bEOM :: B.ByteString
bEOM = B.singleton eOM

-- | Valid keys in the requests and responses.
data MsgKeys = Method
             | Args
             | Success
             | Result

-- | The serialisation of MsgKeys into strings in messages.
$(genStrOfKey ''MsgKeys "strOfKey")


-- | Luxi client encapsulation.
data Client = Client { socket :: Handle           -- ^ The socket of the client
                     , rbuf :: IORef B.ByteString -- ^ Already received buffer
                     }

-- | Connects to the master daemon and returns a luxi Client.
getLuxiClient :: String -> IO Client
getLuxiClient path = do
  s <- S.socket S.AF_UNIX S.Stream S.defaultProtocol
  withTimeout luxiDefCtmo "creating luxi connection" $
              S.connect s (S.SockAddrUnix path)
  rf <- newIORef B.empty
  h <- S.socketToHandle s ReadWriteMode
  return Client { socket=h, rbuf=rf }

-- | Creates and returns a server endpoint.
getLuxiServer :: Bool -> FilePath -> IO S.Socket
getLuxiServer setOwner path = do
  s <- S.socket S.AF_UNIX S.Stream S.defaultProtocol
  S.bindSocket s (S.SockAddrUnix path)
  when setOwner . setOwnerAndGroupFromNames path GanetiLuxid $
    ExtraGroup DaemonsGroup
  S.listen s 5 -- 5 is the max backlog
  return s

-- | Closes a server endpoint.
-- FIXME: this should be encapsulated into a nicer type.
closeServer :: FilePath -> S.Socket -> IO ()
closeServer path sock = do
  S.sClose sock
  removeFile path

-- | Accepts a client
acceptClient :: S.Socket -> IO Client
acceptClient s = do
  -- second return is the address of the client, which we ignore here
  (client_socket, _) <- S.accept s
  new_buffer <- newIORef B.empty
  handle <- S.socketToHandle client_socket ReadWriteMode
  return Client { socket=handle, rbuf=new_buffer }

-- | Closes the client socket.
closeClient :: Client -> IO ()
closeClient = hClose . socket

-- | Sends a message over a luxi transport.
sendMsg :: Client -> String -> IO ()
sendMsg s buf = withTimeout luxiDefRwto "sending luxi message" $ do
  let encoded = UTF8L.fromString buf
      handle = socket s
  BL.hPut handle encoded
  B.hPut handle bEOM
  hFlush handle

-- | Given a current buffer and the handle, it will read from the
-- network until we get a full message, and it will return that
-- message and the leftover buffer contents.
recvUpdate :: Handle -> B.ByteString -> IO (B.ByteString, B.ByteString)
recvUpdate handle obuf = do
  nbuf <- withTimeout luxiDefRwto "reading luxi response" $ do
            _ <- hWaitForInput handle (-1)
            B.hGetNonBlocking handle 4096
  let (msg, remaining) = B.break (eOM ==) nbuf
      newbuf = B.append obuf msg
  if B.null remaining
    then recvUpdate handle newbuf
    else return (newbuf, B.tail remaining)

-- | Waits for a message over a luxi transport.
recvMsg :: Client -> IO String
recvMsg s = do
  cbuf <- readIORef $ rbuf s
  let (imsg, ibuf) = B.break (eOM ==) cbuf
  (msg, nbuf) <-
    if B.null ibuf      -- if old buffer didn't contain a full message
      then recvUpdate (socket s) cbuf   -- then we read from network
      else return (imsg, B.tail ibuf)   -- else we return data from our buffer
  writeIORef (rbuf s) nbuf
  return $ UTF8.toString msg

-- | Extended wrapper over recvMsg.
recvMsgExt :: Client -> IO RecvResult
recvMsgExt s =
  Control.Exception.catch (liftM RecvOk (recvMsg s)) $ \e ->
    return $ if isEOFError e
               then RecvConnClosed
               else RecvError (show e)


-- | Serialize the response to String.
buildResponse :: Bool    -- ^ Success
              -> JSValue -- ^ The arguments
              -> String  -- ^ The serialized form
buildResponse success args =
  let ja = [ (strOfKey Success, JSBool success)
           , (strOfKey Result, args)]
      jo = toJSObject ja
  in encodeStrict jo