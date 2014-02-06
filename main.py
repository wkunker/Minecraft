import math
import random
import time
import thread
import sys
import pygletreactor
pygletreactor.install()

import jsonpickle

from twisted.spread import pb
from twisted.internet import reactor
from twisted.python import util

from collections import deque
from pyglet import image
from pyglet.gl import *
from pyglet.graphics import TextureGroup
from pyglet.window import key, mouse
from abc import ABCMeta, abstractmethod

import __builtin__

__builtin__.WINDOW = False

TICKS_PER_SEC = 60

# Size of sectors used to ease block loading.
SECTOR_SIZE = 16

WALKING_SPEED = 5
ACTUAL_WALKING_SPEED = WALKING_SPEED
FLYING_SPEED = 15

GRAVITY = 20.0
MAX_JUMP_HEIGHT = 1.0 # About the height of a block.
# To derive the formula for calculating jump speed, first solve
#    v_t = v_0 + a * t
# for the time at which you achieve maximum height, where a is the acceleration
# due to gravity and v_t = 0. This gives:
#    t = - v_0 / a
# Use t and the desired MAX_JUMP_HEIGHT to solve for v_0 (jump speed) in
#    s = s_0 + v_0 * t + (a * t^2) / 2
JUMP_SPEED = math.sqrt(2 * GRAVITY * MAX_JUMP_HEIGHT)
TERMINAL_VELOCITY = 50

PLAYER_HEIGHT = 2

def cube_vertices(x, y, z, n):
    """ Return the vertices of the cube at position x, y, z with size 2*n.

    """
    return [
        x-n,y+n,z-n, x-n,y+n,z+n, x+n,y+n,z+n, x+n,y+n,z-n,  # top
        x-n,y-n,z-n, x+n,y-n,z-n, x+n,y-n,z+n, x-n,y-n,z+n,  # bottom
        x-n,y-n,z-n, x-n,y-n,z+n, x-n,y+n,z+n, x-n,y+n,z-n,  # left
        x+n,y-n,z+n, x+n,y-n,z-n, x+n,y+n,z-n, x+n,y+n,z+n,  # right
        x-n,y-n,z+n, x+n,y-n,z+n, x+n,y+n,z+n, x-n,y+n,z+n,  # front
        x+n,y-n,z-n, x-n,y-n,z-n, x-n,y+n,z-n, x+n,y+n,z-n,  # back
    ]


def tex_coord(x, y, n=1):
    """ Return the bounding vertices of the texture square.

    """
    m = 1.0 / n
    dx = x * m
    dy = y * m
    return dx, dy, dx + m, dy, dx + m, dy + m, dx, dy + m


def tex_coords(top=(0,0), bottom=(0,0), side=(0,0)):
    """ Return a list of the texture squares for the top, bottom and side.

    """
    top = tex_coord(*top)
    bottom = tex_coord(*bottom)
    side = tex_coord(*side)
    result = []
    result.extend(top)
    result.extend(bottom)
    result.extend(side * 4)
    return result


# The TextureGroupManager classes are responsible for ensuring
#   that only one copy of a TextureGroup exists for any given texture.
class TextureGroupManager_BaseTextureGroup(object):
    def __init__(self, filename, name):
        self.name = name
        self.filename = filename
        self.group = TextureGroup(image.load(filename).get_texture())

class TextureGroupManager(object):
    def __init__(self):
        self.textures = []
    # Attempts to create a TextureGroup from a specified file if it isn't already available.
    # If it is, it returns the existing TextureGroup instead.
    # Attempts to match by name, if possible--If not available, reverts to filename.
    def loadTexture(self, filename, name=False):
        if(name == False):
            name = filename
        for t in self.textures:
            if(name == t.name):
                return t
        x = TextureGroupManager_BaseTextureGroup(filename, name)
        self.textures.append(x)
        return x
textureGroupManager = TextureGroupManager()

# An instance of Block exists for each available block type.
# Attributes of Block are shared where it's necessary to optimize.
class Block(object):
    def __init__(self, texture_file):
        self.texture_coords = tex_coords()
        self.baseTextureGroup = textureGroupManager.loadTexture(texture_file)
        self.group = self.baseTextureGroup.group
        self.texture_file = texture_file

BLOCKS = {}
BLOCKS["GRASS"] = Block("grass.png")
BLOCKS["SAND"] = Block("sand.png")
BLOCKS["BRICK"] = Block("brick.png")
BLOCKS["STONE"] = Block("stone.png")
BLOCKS["WOOD"] = Block("wood.png")
BLOCKS["STICK"] = Block("stick.png")
BLOCKS["COAL"] = Block("coal.png")

RECIPES = {}
RECIPES["stick"] = {"column": [[BLOCKS["WOOD"], BLOCKS["WOOD"]], [], [], []], "result": BLOCKS["STICK"]} # 2 wood blocks stacked on top of each other.

FACES = [
    ( 0, 1, 0),
    ( 0,-1, 0),
    (-1, 0, 0),
    ( 1, 0, 0),
    ( 0, 0, 1),
    ( 0, 0,-1),
]


def normalize(position):
    """ Accepts `position` of arbitrary precision and returns the block
    containing that position.

    Parameters
    ----------
    position : tuple of len 3

    Returns
    -------
    block_position : tuple of ints of len 3

    """
    x, y, z = position
    x, y, z = (int(round(x)), int(round(y)), int(round(z)))
    return (x, y, z)


def sectorize(position):
    """ Returns a tuple representing the sector for the given `position`.

    Parameters
    ----------
    position : tuple of len 3

    Returns
    -------
    sector : tuple of len 3

    """
    x, y, z = normalize(position)
    x, y, z = x / SECTOR_SIZE, y / SECTOR_SIZE, z / SECTOR_SIZE
    return (x, 0, z)


class Model(object):

    def __init__(self):

        # A Batch is a collection of vertex lists for batched rendering.
        self.batch = pyglet.graphics.Batch()

        # A mapping from position to the texture of the block at that position.
        # This defines all the blocks that are currently in the world.
        self.world = {}

        # Same mapping as `world` but only contains blocks that are shown.
        self.shown = {}

        # Mapping from position to a pyglet `VertextList` for all shown blocks.
        self._shown = {}

        # Mapping from sector to a list of positions inside that sector.
        self.sectors = {}

        # Simple function queue implementation. The queue is populated with
        # _show_block() and _hide_block() calls
        self.queue = deque()

        self._initialize()

    def _initialize(self):
        """ Initialize the world by placing all the blocks.

        """
        n = 80  # 1/2 width and height of world
        s = 1  # step size
        y = -100  # initial y height
        max_depth = 10
        h = 0
        while h < max_depth:
            h += 1

            '''max_aint = 50
            aint = random.randint(0, max_aint)
            if aint <= 1: # 1% chance to switch terrain placement height.
                bint = random.randint(0, 1)
                cint = random.randint(0, 1)
                if cint == 0:
                    y -= bint
                else:
                    y += bint'''

            for x in xrange(-n, n + 1, s):
                for z in xrange(-n, n + 1, s):
                    # create a layer stone an grass everywhere.                    
                    rint = random.randint(0, 500)
                    coal_drop_rate = 2 + h
                    if rint >= coal_drop_rate:
                        self.add_block((x, y - h, z), BLOCKS["GRASS"], immediate=False)
                    elif rint >= coal_drop_rate + 200:
                        self.add_block((x, y - h, z), BLOCKS["STONE"], immediate=False)
                    else:
                        self.add_block((x, y - h, z), BLOCKS["COAL"], immediate=False)

                    if x in (-n, n) or z in (-n, n):
                        # create outer walls.
                        for dy in xrange(-2, 3):
                            self.add_block((x, y + dy, z), BLOCKS["STONE"], immediate=False)

        # generate the hills randomly
        '''
        o = n - 10
        for _ in xrange(120):
            a = random.randint(-o, o)  # x position of the hill
            b = random.randint(-o, o)  # z position of the hill
            c = -1  # base of the hill
            h = random.randint(1, 6)  # height of the hill
            s = random.randint(4, 8)  # 2 * s is the side length of the hill
            d = 1  # how quickly to taper off the hills
            t_rand = random.randint(1, 100)
            if t_rand < 21:
                t = BLOCKS["GRASS"]
            elif t_rand < 61:
                t = BLOCKS["SAND"]
            elif t_rand < 81:
                t = BLOCKS["BRICK"]
            else:
                t = BLOCKS["WOOD"]

            #t = random.choice([BLOCKS["GRASS"], BLOCKS["SAND"], BLOCKS["BRICK"], BLOCKS["WOOD"]])
            for y in xrange(c, c + h):
                for x in xrange(a - s, a + s + 1):
                    for z in xrange(b - s, b + s + 1):
                        if (x - a) ** 2 + (z - b) ** 2 > (s + 1) ** 2:
                            continue
                        if (x - 0) ** 2 + (z - 0) ** 2 < 5 ** 2:
                            continue
                        self.add_block((x, y, z), t, immediate=False)
                s -= d  # decrement side lenth so hills taper off
        '''
    def hit_test(self, position, vector, max_distance=8):
        """ Line of sight search from current position. If a block is
        intersected it is returned, along with the block previously in the line
        of sight. If no block is found, return None, None.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position to check visibility from.
        vector : tuple of len 3
            The line of sight vector.
        max_distance : int
            How many blocks away to search for a hit.

        """
        m = 8
        x, y, z = position
        dx, dy, dz = vector
        previous = None
        for _ in xrange(max_distance * m):
            key = normalize((x, y, z))
            if key != previous and key in self.world:
                return key, previous
            previous = key
            x, y, z = x + dx / m, y + dy / m, z + dz / m
        return None, None

    def exposed(self, position):
        """ Returns False is given `position` is surrounded on all 6 sides by
        blocks, True otherwise.

        """
        x, y, z = position
        for dx, dy, dz in FACES:
            if (x + dx, y + dy, z + dz) not in self.world:
                return True
        return False

    def add_block(self, position, texture, immediate=True):
        """ Add a block with the given `texture` and `position` to the world.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to add.
        texture : list of len 3
            The coordinates of the texture squares. Use `tex_coords()` to
            generate.
        immediate : bool
            Whether or not to draw the block immediately.

        """
        if position in self.world:
            self.remove_block(position, immediate)
        self.world[position] = texture
        self.sectors.setdefault(sectorize(position), []).append(position)
        if immediate:
            if self.exposed(position):
                self.show_block(position)
            self.check_neighbors(position)

    def remove_block(self, position, immediate=True):
        """ Remove the block at the given `position`.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to remove.
        immediate : bool
            Whether or not to immediately remove block from canvas.

        """
        del self.world[position]
        self.sectors[sectorize(position)].remove(position)
        if immediate:
            if position in self.shown:
                self.hide_block(position)
            self.check_neighbors(position)

    def check_neighbors(self, position):
        """ Check all blocks surrounding `position` and ensure their visual
        state is current. This means hiding blocks that are not exposed and
        ensuring that all exposed blocks are shown. Usually used after a block
        is added or removed.

        """
        x, y, z = position
        for dx, dy, dz in FACES:
            key = (x + dx, y + dy, z + dz)
            if key not in self.world:
                continue
            if self.exposed(key):
                if key not in self.shown:
                    self.show_block(key)
            else:
                if key in self.shown:
                    self.hide_block(key)

    def show_block(self, position, immediate=True):
        """ Show the block at the given `position`. This method assumes the
        block has already been added with add_block()

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to show.
        immediate : bool
            Whether or not to show the block immediately.

        """
        texture = self.world[position].texture_coords
        group = self.world[position].group
        self.shown[position] = texture
        if immediate:
            self._show_block(position, group, texture)
        else:
            self._enqueue(self._show_block, position, group, texture)

    def _show_block(self, position, group, texture):
        """ Private implementation of the `show_block()` method.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to show.
        texture : list of len 3
            The coordinates of the texture squares. Use `tex_coords()` to
            generate.

        """
        x, y, z = position
        vertex_data = cube_vertices(x, y, z, 0.5)
        texture_data = list(texture)
        # create vertex list
        # FIXME Maybe `add_indexed()` should be used instead
        self._shown[position] = self.batch.add(24, GL_QUADS, group,
            ('v3f/static', vertex_data),
            ('t2f/static', texture_data))

    def hide_block(self, position, immediate=True):
        """ Hide the block at the given `position`. Hiding does not remove the
        block from the world.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to hide.
        immediate : bool
            Whether or not to immediately remove the block from the canvas.

        """
        self.shown.pop(position)
        if immediate:
            self._hide_block(position)
        else:
            self._enqueue(self._hide_block, position)

    def _hide_block(self, position):
        """ Private implementation of the 'hide_block()` method.

        """
        self._shown.pop(position).delete()

    def show_sector(self, sector):
        """ Ensure all blocks in the given sector that should be shown are
        drawn to the canvas.

        """
        for position in self.sectors.get(sector, []):
            if position not in self.shown and self.exposed(position):
                self.show_block(position, False)

    def hide_sector(self, sector):
        """ Ensure all blocks in the given sector that should be hidden are
        removed from the canvas.

        """
        for position in self.sectors.get(sector, []):
            if position in self.shown:
                self.hide_block(position, False)

    def change_sectors(self, before, after):
        """ Move from sector `before` to sector `after`. A sector is a
        contiguous x, y sub-region of world. Sectors are used to speed up
        world rendering.

        """
        before_set = set()
        after_set = set()
        pad = 4
        for dx in xrange(-pad, pad + 1):
            for dy in [0]:  # xrange(-pad, pad + 1):
                for dz in xrange(-pad, pad + 1):
                    if dx ** 2 + dy ** 2 + dz ** 2 > (pad + 1) ** 2:
                        continue
                    if before:
                        x, y, z = before
                        before_set.add((x + dx, y + dy, z + dz))
                    if after:
                        x, y, z = after
                        after_set.add((x + dx, y + dy, z + dz))
        show = after_set - before_set
        hide = before_set - after_set
        for sector in show:
            self.show_sector(sector)
        for sector in hide:
            self.hide_sector(sector)

    def _enqueue(self, func, *args):
        """ Add `func` to the internal queue.

        """
        self.queue.append((func, args))

    def _dequeue(self):
        """ Pop the top function from the internal queue and call it.

        """
        func, args = self.queue.popleft()
        func(*args)

    def process_queue(self):
        """ Process the entire queue while taking periodic breaks. This allows
        the game loop to run smoothly. The queue contains calls to
        _show_block() and _hide_block() so this method should be called if
        add_block() or remove_block() was called with immediate=False

        """
        start = time.clock()
        while self.queue and time.clock() - start < 1.0 / TICKS_PER_SEC:
            self._dequeue()

    def process_entire_queue(self):
        """ Process the entire queue with no breaks.

        """
        while self.queue:
            self._dequeue()



class WorldItems(object):

    def __init__(self):

        # A Batch is a collection of vertex lists for batched rendering.
        self.batch = pyglet.graphics.Batch()

        # A mapping from position to the texture of the block at that position.
        # This defines all the blocks that are currently in the world.
        self.world = {}

        # Same mapping as `world` but only contains blocks that are shown.
        self.shown = {}

        # Mapping from position to a pyglet `VertextList` for all shown blocks.
        self._shown = {}

        # Mapping from sector to a list of positions inside that sector.
        self.sectors = {}

        # Simple function queue implementation. The queue is populated with
        # _show_block() and _hide_block() calls
        self.queue = deque()

        self._initialize()

    def _initialize(self):
        pass

    def hit_test(self, position, vector, max_distance=8):
        """ Line of sight search from current position. If a block is
        intersected it is returned, along with the block previously in the line
        of sight. If no block is found, return None, None.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position to check visibility from.
        vector : tuple of len 3
            The line of sight vector.
        max_distance : int
            How many blocks away to search for a hit.

        """
        m = 8
        x, y, z = position
        dx, dy, dz = vector
        previous = None
        for _ in xrange(max_distance * m):
            key = normalize((x, y, z))
            if key != previous and key in self.world:
                return key, previous
            previous = key
            x, y, z = x + dx / m, y + dy / m, z + dz / m
        return None, None

    def exposed(self, position):
        """ Returns False is given `position` is surrounded on all 6 sides by
        blocks, True otherwise.

        """
        x, y, z = position
        for dx, dy, dz in FACES:
            if (x + dx, y + dy, z + dz) not in self.world:
                return True
        return False

    def add_block(self, position, texture, immediate=True):
        """ Add a block with the given `texture` and `position` to the world.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to add.
        texture : list of len 3
            The coordinates of the texture squares. Use `tex_coords()` to
            generate.
        immediate : bool
            Whether or not to draw the block immediately.

        """
        if position in self.world:
            self.remove_block(position, immediate)
        self.world[position] = texture
        self.sectors.setdefault(sectorize(position), []).append(position)
        if immediate:
            if self.exposed(position):
                self.show_block(position)
            self.check_neighbors(position)

    def remove_block(self, position, immediate=True):
        """ Remove the block at the given `position`.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to remove.
        immediate : bool
            Whether or not to immediately remove block from canvas.

        """
        del self.world[position]
        self.sectors[sectorize(position)].remove(position)
        if immediate:
            if position in self.shown:
                self.hide_block(position)
            self.check_neighbors(position)

    def check_neighbors(self, position):
        """ Check all blocks surrounding `position` and ensure their visual
        state is current. This means hiding blocks that are not exposed and
        ensuring that all exposed blocks are shown. Usually used after a block
        is added or removed.

        """
        x, y, z = position
        for dx, dy, dz in FACES:
            key = (x + dx, y + dy, z + dz)
            if key not in self.world:
                continue
            if self.exposed(key):
                if key not in self.shown:
                    self.show_block(key)
            else:
                if key in self.shown:
                    self.hide_block(key)

    def show_block(self, position, immediate=True):
        """ Show the block at the given `position`. This method assumes the
        block has already been added with add_block()

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to show.
        immediate : bool
            Whether or not to show the block immediately.

        """
        texture = self.world[position].texture_coords
        group = self.world[position].group
        self.shown[position] = texture
        if immediate:
            self._show_block(position, group, texture)
        else:
            self._enqueue(self._show_block, position, group, texture)

    def _show_block(self, position, group, texture):
        """ Private implementation of the `show_block()` method.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to show.
        texture : list of len 3
            The coordinates of the texture squares. Use `tex_coords()` to
            generate.

        """
        x, y, z = position
        vertex_data = cube_vertices(x, y, z, 0.1)
        texture_data = list(texture)
        # create vertex list
        # FIXME Maybe `add_indexed()` should be used instead
        self._shown[position] = self.batch.add(24, GL_QUADS, group,
            ('v3f/static', vertex_data),
            ('t2f/static', texture_data))

    def hide_block(self, position, immediate=True):
        """ Hide the block at the given `position`. Hiding does not remove the
        block from the world.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position of the block to hide.
        immediate : bool
            Whether or not to immediately remove the block from the canvas.

        """
        self.shown.pop(position)
        if immediate:
            self._hide_block(position)
        else:
            self._enqueue(self._hide_block, position)

    def _hide_block(self, position):
        """ Private implementation of the 'hide_block()` method.

        """
        self._shown.pop(position).delete()

    def show_sector(self, sector):
        """ Ensure all blocks in the given sector that should be shown are
        drawn to the canvas.

        """
        for position in self.sectors.get(sector, []):
            if position not in self.shown and self.exposed(position):
                self.show_block(position, False)

    def hide_sector(self, sector):
        """ Ensure all blocks in the given sector that should be hidden are
        removed from the canvas.

        """
        for position in self.sectors.get(sector, []):
            if position in self.shown:
                self.hide_block(position, False)

    def change_sectors(self, before, after):
        """ Move from sector `before` to sector `after`. A sector is a
        contiguous x, y sub-region of world. Sectors are used to speed up
        world rendering.

        """
        before_set = set()
        after_set = set()
        pad = 4
        for dx in xrange(-pad, pad + 1):
            for dy in [0]:  # xrange(-pad, pad + 1):
                for dz in xrange(-pad, pad + 1):
                    if dx ** 2 + dy ** 2 + dz ** 2 > (pad + 1) ** 2:
                        continue
                    if before:
                        x, y, z = before
                        before_set.add((x + dx, y + dy, z + dz))
                    if after:
                        x, y, z = after
                        after_set.add((x + dx, y + dy, z + dz))
        show = after_set - before_set
        hide = before_set - after_set
        for sector in show:
            self.show_sector(sector)
        for sector in hide:
            self.hide_sector(sector)

    def _enqueue(self, func, *args):
        """ Add `func` to the internal queue.

        """
        self.queue.append((func, args))

    def _dequeue(self):
        """ Pop the top function from the internal queue and call it.

        """
        func, args = self.queue.popleft()
        func(*args)

    def process_queue(self):
        # Check to see if world item is in range of the player.
        #pos_rounded = (int(round(WINDOW.position[0])), int(round(WINDOW.position[1])), int(round(WINDOW.position[2])))
        #print str(pos_rounded)

        #item = False
        #for k,v in self.world.iteritems():
        #    if k[0] == pos_rounded[0] and k[2] == pos_rounded[2]:
        #        item = getInventoryItemBlockFromWorldBlockPosition(k)
        #        break
        #item = getInventoryItemBlockFromWorldBlockPosition()

        #if(item != False):
        #    #config.InventoryItem_MultiTool_use(params, item)
        #    WINDOW.player.inventory.add(item)
        #    self.remove_block(WINDOW.position)

        """ Process the entire queue while taking periodic breaks. This allows
        the game loop to run smoothly. The queue contains calls to
        _show_block() and _hide_block() so this method should be called if
        add_block() or remove_block() was called with immediate=False

        """
        start = time.clock()
        while self.queue and time.clock() - start < 1.0 / TICKS_PER_SEC:
            self._dequeue()

    def process_entire_queue(self):
        """ Process the entire queue with no breaks.

        """
        while self.queue:
            self._dequeue()



class MenuItem(object):
    def __init__(self, window, image_file, pos_x, pos_y):
        self.image_file = image_file
        self.pos_x = pos_x
        self.pos_y = pos_y
        item_image = pyglet.image.load(image_file)
        item = pyglet.sprite.Sprite(item_image, x=pos_x, y=pos_y)
        window.drawregister.add(item.draw)
        self.item_draw = item.draw

class MenuItemManager(object):
    def __init__(self, window):
        self.window = window
        self.items = []
        self.menu_position_x = 50
        self.menu_position_y = 50
        self.menu_item_size_x = 80
        self.menu_item_size_y = 80

    def addItem(self, image, inventory_position):
        for k,i in enumerate(self.items):
            if i.image_file == "empty.png":
                self.items[k] = MenuItem(self.window, image, inventory_position * self.menu_item_size_x + self.menu_position_x, i.pos_y)
                return k
        item_x = inventory_position * self.menu_item_size_x + self.menu_position_x
        item_y = self.menu_position_y
        self.items.append(MenuItem(self.window, image, item_x, item_y))
        return inventory_position
        #return len(self.items) ## was this before

    def removeItem(self, index):
        self.window.drawregister.remove(self.items[index].item_draw)
        self.items[index] = MenuItem(self.window, "empty.png", self.items[index].pos_x, self.items[index].pos_y)

    def findItem(self, image):
        for i in self.items:
            if i.image_file == image:
                return True
        return False

class UI(object):
    def __init__(self, window):
        self.window = window
        self.itemkeypressed = []
        self.menu_item_manager = MenuItemManager(window)

    def informItemKeyPressed(self, keyNum):
        self.test = pyglet.text.Label(
            str(keyNum),
            font_name='Times New Roman',
            font_size=36,
            x=self.window.width//2, y=self.window.height//2,
            anchor_x='center', anchor_y='center')

        for el in self.itemkeypressed:
            self.window.drawregister.remove(el)
            self.itemkeypressed.remove(el)

        self.window.drawregister.add(self.test.draw)
        self.window.drawregister.removeAfter(self.test.draw, 1)
        self.itemkeypressed.append(self.test.draw)

class DrawRegister(object):
    def __init__(self):
        self.drawregister = []

    # func is the function passed as an object, to be added to the draw register.
    # exceptions handled internally.
    def add(self, func):
        self.drawregister.append(func)

    # func is the function passed as an object,
    #   to be removed 's' seconds after this is called.
    def removeAfter(self, func, s):
        def waitAndRemove():
            time.sleep(s)
            try:
                self.remove(func)
            except:
                pass
        thread.start_new_thread(waitAndRemove, ())

    def remove(self, func):
        try:
            self.drawregister.remove(func)
        except:
            pass

# All inventory items can be "dropped" and "used"
class InventoryItem(object):
    # max_qty represents the max items that can fit in a stack.
    def __init__(self, name, max_qty, ui_texture):
        self.name = name
        self.max_qty = max_qty
        self.qty = 1 # Default quantity is 1
        self.ui_texture = ui_texture
    @abstractmethod
    def use(self, params):
        pass
    # Drop always behaves in the same way, so it's not abstract.
    def drop(self):
        blk = BLOCKS.get(self.name)
        if(blk == None):
            BLOCKS[self.name] = Block(self.ui_texture)
            BLOCKS[self.name].inventory_item = self
        vector = WINDOW.get_sight_vector()
        block, previous = WINDOW.model.hit_test(WINDOW.position, vector)
        if(previous):
            WINDOW.world_items.add_block(previous, blk)
            WINDOW.player.inventory.remove(self)
            WINDOW.player.selected = WINDOW.player.inventory.findNewSelected()

class InventoryItem_Block(InventoryItem):
    def __init__(self, blocktype, name=False):
        if(name == False):
            name = blocktype
        # Max quantity of a stack of blocks is 64.
        self.worldblock = BLOCKS[blocktype]
        try:
            self.inventory_item = BLOCKS[blocktype].inventory_item
        except:
            pass
        super(InventoryItem_Block, self).__init__(name, 64, self.worldblock.texture_file)
    def use(self, params):
        # Place the block
        if(self.qty > 0):
            WINDOW.model.add_block(params, self.worldblock)
            self.qty -= 1
            if self.qty <= 0:
                WINDOW.player.inventory.remove(self)
        else:
            WINDOW.player.inventory.remove(self)
        WINDOW.player.selected = WINDOW.player.inventory.findNewSelected()

def getInventoryItemBlockFromWorldBlockPosition(worldblockposition):
    blocks = {}
    for k,v in BLOCKS.iteritems():
        blocks[k] = InventoryItem_Block(k)
    try:
        for k,v in blocks.iteritems():
            if(WINDOW.model.world[worldblockposition] == BLOCKS[k]):
                return blocks[k]
    except:
        return False
    return False

def getInventoryItemBlockFromWorldItemPosition(worlditemposition):
    blocks = {}
    for k,v in BLOCKS.iteritems():
        blocks[k] = InventoryItem_Block(k)
    try:
        for k,v in blocks.iteritems():
            if(WINDOW.world_items.world[worlditemposition] == BLOCKS[k]):
                return blocks[k]
    except:
        return False
    return False

class InventoryItem_MultiTool(InventoryItem):
    def __init__(self, name="MultiTool"):
        # Max quantity of a stack of multi-tools is 1.
        super(InventoryItem_MultiTool, self).__init__(name, 1, "picaxe.png")

    def use(self, params):
        item = getInventoryItemBlockFromWorldBlockPosition(params)
        if(item != False):
            WINDOW.player.inventory.add(item)
            WINDOW.model.remove_block(params)

class InventoryItem_AssemblerTool(InventoryItem):
    def __init__(self, name="AssemblerTool"):
        # Max quantity of a stack of multi-tools is 1.
        super(InventoryItem_AssemblerTool, self).__init__(name, 1, "assembler.png")

    # Use on the bottom-left block of the blocks-to-be-assembled.
    def use(self, params):
        #item = getInventoryItemBlockFromWorldBlockPosition(params)
        #if(item != False):
        #config.InventoryItem_AssemblerTool_use(params, item)
        for k,v in RECIPES.iteritems():
            z = 0
            success = True
            all_pos = []
            # Compare the stack of blocks against the current recipe
            for y in v["column"][0]:
                p = params[0], params[1] + z, params[2]
                if WINDOW.model.world[p] != y:
                    success = False
                    break # break back to the main RECIPES for loop
                all_pos.append(p)
                z += 1
            if success == True:
                # This must be the recipe -- make it by destroying the input blocks, and creating the output item
                for i in all_pos:
                    WINDOW.model.remove_block(i)
                ###### getInventoryItemBlockFromWorldBlockPosition(params)
                WINDOW.world_items.add_block(params, v["result"])
                return



# Contains common inventory logic, such as stacking
#   and unstacking inventory items.
class Inventory(object):
    def __init__(self, window):
        self.window = window
        self.inventory = []
        a = 0
        while a < 9:
            a += 1
            self.inventory.append(False)
    def add(self, item, qty=1):
        for k,i in enumerate(self.inventory):
            if(i == False):
                item.ui_position = self.window.UI.menu_item_manager.addItem(item.ui_texture, k)
                self.inventory[k] = item
                return
        #self.inventory.append(item)
        return
    def remove(self, item, qty=1):
        for a,i in enumerate(self.inventory):
            if(i != False):
                if(i.name == item.name):
                    self.window.UI.menu_item_manager.removeItem(a)
                    self.inventory[a] = False
                    return
    def findNewSelected(self):
        for k,i in enumerate(self.inventory):
            if(i != False):
                return i
        return False

class Player(object):
    def __init__(self, window):
        #self.inventory = [InventoryItem_Block("BRICK"), InventoryItem_Block("GRASS"), InventoryItem_Block("SAND")]
        self.inventory = Inventory(window)
        self.inventory.add(InventoryItem_MultiTool())
        self.inventory.add(InventoryItem_AssemblerTool())
        self.selected = self.inventory.findNewSelected()
        self.window = window
    def pickup(self):
        vector = self.window.get_sight_vector()
        block, previous = self.window.model.hit_test(self.window.position, vector)
        try:
            item = getInventoryItemBlockFromWorldItemPosition(previous).inventory_item
        except:
            item = getInventoryItemBlockFromWorldItemPosition(previous)
        if(item != False):
            self.window.player.inventory.add(item)
            self.window.world_items.remove_block(previous)

# Represents any player which is not the current player.
class NetworkPlayer(object):
    def __init__(self, position):
        self.__firstrun = True
        self.setPosition(position)
        self.__firstrun = False
        self.strafe = 0
    def setPosition(self, position):
        if(self.__firstrun == False):
            WINDOW.model.remove_block(self._position, immediate=True)
        self._position = position
        WINDOW.model.add_block(position, BLOCKS["BRICK"], immediate=True)

    def getPosition(self):
        return self._position

    def update(self, dt):
        m = 8
        dt = min(dt, 0.2)
        for _ in xrange(m):
            self._update(dt / m)

    def _update(self, dt):
        # walking
        speed = FLYING_SPEED if self.flying else ACTUAL_WALKING_SPEED
        d = dt * speed # distance covered this tick.
        dx, dy, dz = self.get_motion_vector()
        # New position in space, before accounting for gravity.
        dx, dy, dz = dx * d, dy * d, dz * d
        # gravity
        if not self.flying:
            # Update your vertical speed: if you are falling, speed up until you
            # hit terminal velocity; if you are jumping, slow down until you
            # start falling.
            self.dy -= dt * GRAVITY
            self.dy = max(self.dy, -TERMINAL_VELOCITY)
            dy += self.dy * dt
        # collisions
        x, y, z = self.position
        x, y, z = self.collide((x + dx, y + dy, z + dz), PLAYER_HEIGHT)
        self.position = (x, y, z)

    def get_motion_vector(self):
        """ Returns the current motion vector indicating the velocity of the
        NetworkPlayer.

        Returns
        -------
        vector : tuple of len 3
            Tuple containing the velocity in x, y, and z respectively.

        """
        if any(self.strafe):
            x, y = self.rotation
            strafe = math.degrees(math.atan2(*self.strafe))
            y_angle = math.radians(y)
            x_angle = math.radians(x + strafe)
            if self.flying:
                m = math.cos(y_angle)
                dy = math.sin(y_angle)
                if self.strafe[1]:
                    # Moving left or right.
                    dy = 0.0
                    m = 1
                if self.strafe[0] > 0:
                    # Moving backwards.
                    dy *= -1
                # When you are flying up or down, you have less left and right
                # motion.
                dx = math.cos(x_angle) * m
                dz = math.sin(x_angle) * m
            else:
                dy = 0.0
                dx = math.cos(x_angle)
                dz = math.sin(x_angle)
        else:
            dy = 0.0
            dx = 0.0
            dz = 0.0
        return (dx, dy, dz)

class Window(pyglet.window.Window):

    def __init__(self, *args, **kwargs):
        super(Window, self).__init__(*args, **kwargs)

        # Whether or not the window exclusively captures the mouse.
        self.exclusive = False

        # When flying gravity has no effect and speed is increased.
        self.flying = False

        # Strafing is moving lateral to the direction you are facing,
        # e.g. moving to the left or right while continuing to face forward.
        #
        # First element is -1 when moving forward, 1 when moving back, and 0
        # otherwise. The second element is -1 when moving left, 1 when moving
        # right, and 0 otherwise.
        self.strafe = [0, 0]

        # Current (x, y, z) position in the world, specified with floats. Note
        # that, perhaps unlike in math class, the y-axis is the vertical axis.
        self.position = (0, 0, 0)

        # First element is rotation of the player in the x-z plane (ground
        # plane) measured from the z-axis down. The second is the rotation
        # angle from the ground plane up. Rotation is in degrees.
        #
        # The vertical plane rotation ranges from -90 (looking straight down) to
        # 90 (looking straight up). The horizontal rotation range is unbounded.
        self.rotation = (0, 0)

        # Which sector the player is currently in.
        self.sector = None

        # The crosshairs at the center of the screen.
        self.reticle = None

        # Velocity in the y (upward) direction.
        self.dy = 0

        # Convenience list of num keys.
        self.num_keys = [
            key._1, key._2, key._3, key._4, key._5,
            key._6, key._7, key._8, key._9, key._0]

        # Instance of the model that handles the world.
        self.model = Model()

        # The label that is displayed in the top left of the canvas.
        self.label = pyglet.text.Label('', font_name='Arial', font_size=18,
            x=10, y=self.height - 10, anchor_x='left', anchor_y='top',
            color=(0, 0, 0, 255))

        self.drawregister = DrawRegister()

        # This call schedules the `update()` method to be called
        # TICKS_PER_SEC. This is the main game event loop.
        pyglet.clock.schedule_interval(self.update, 1.0 / TICKS_PER_SEC)

        self.UI = UI(self)
        self.player = Player(self)
        self.world_items = WorldItems()

    def set_exclusive_mouse(self, exclusive):
        """ If `exclusive` is True, the game will capture the mouse, if False
        the game will ignore the mouse.

        """
        super(Window, self).set_exclusive_mouse(exclusive)
        self.exclusive = exclusive

    def get_sight_vector(self):
        """ Returns the current line of sight vector indicating the direction
        the player is looking.

        """
        x, y = self.rotation
        # y ranges from -90 to 90, or -pi/2 to pi/2, so m ranges from 0 to 1 and
        # is 1 when looking ahead parallel to the ground and 0 when looking
        # straight up or down.
        m = math.cos(math.radians(y))
        # dy ranges from -1 to 1 and is -1 when looking straight down and 1 when
        # looking straight up.
        dy = math.sin(math.radians(y))
        dx = math.cos(math.radians(x - 90)) * m
        dz = math.sin(math.radians(x - 90)) * m
        return (dx, dy, dz)

    def get_motion_vector(self):
        """ Returns the current motion vector indicating the velocity of the
        player.

        Returns
        -------
        vector : tuple of len 3
            Tuple containing the velocity in x, y, and z respectively.

        """
        if any(self.strafe):
            x, y = self.rotation
            strafe = math.degrees(math.atan2(*self.strafe))
            y_angle = math.radians(y)
            x_angle = math.radians(x + strafe)
            if self.flying:
                m = math.cos(y_angle)
                dy = math.sin(y_angle)
                if self.strafe[1]:
                    # Moving left or right.
                    dy = 0.0
                    m = 1
                if self.strafe[0] > 0:
                    # Moving backwards.
                    dy *= -1
                # When you are flying up or down, you have less left and right
                # motion.
                dx = math.cos(x_angle) * m
                dz = math.sin(x_angle) * m
            else:
                dy = 0.0
                dx = math.cos(x_angle)
                dz = math.sin(x_angle)
        else:
            dy = 0.0
            dx = 0.0
            dz = 0.0
        return (dx, dy, dz)

    def update(self, dt):
        """ This method is scheduled to be called repeatedly by the pyglet
        clock.

        Parameters
        ----------
        dt : float
            The change in time since the last call.

        """
        self.model.process_queue()
        self.world_items.process_queue()
        sector = sectorize(self.position)
        if sector != self.sector:
            self.model.change_sectors(self.sector, sector)
            if self.sector is None:
                self.model.process_entire_queue()
            self.sector = sector
        m = 8
        dt = min(dt, 0.2)
        for _ in xrange(m):
            self._update(dt / m)

    def _update(self, dt):
        """ Private implementation of the `update()` method. This is where most
        of the motion logic lives, along with gravity and collision detection.

        Parameters
        ----------
        dt : float
            The change in time since the last call.

        """
        # walking
        speed = FLYING_SPEED if self.flying else ACTUAL_WALKING_SPEED
        d = dt * speed # distance covered this tick.
        dx, dy, dz = self.get_motion_vector()
        # New position in space, before accounting for gravity.
        dx, dy, dz = dx * d, dy * d, dz * d
        # gravity
        if not self.flying:
            # Update your vertical speed: if you are falling, speed up until you
            # hit terminal velocity; if you are jumping, slow down until you
            # start falling.
            self.dy -= dt * GRAVITY
            self.dy = max(self.dy, -TERMINAL_VELOCITY)
            dy += self.dy * dt
        # collisions
        x, y, z = self.position
        x, y, z = self.collide((x + dx, y + dy, z + dz), PLAYER_HEIGHT)
        self.position = (x, y, z)

    def collide(self, position, height):
        """ Checks to see if the player at the given `position` and `height`
        is colliding with any blocks in the world.

        Parameters
        ----------
        position : tuple of len 3
            The (x, y, z) position to check for collisions at.
        height : int or float
            The height of the player.

        Returns
        -------
        position : tuple of len 3
            The new position of the player taking into account collisions.

        """
        # How much overlap with a dimension of a surrounding block you need to
        # have to count as a collision. If 0, touching terrain at all counts as
        # a collision. If .49, you sink into the ground, as if walking through
        # tall grass. If >= .5, you'll fall through the ground.
        pad = 0.25
        p = list(position)
        np = normalize(position)
        for face in FACES:  # check all surrounding blocks
            for i in xrange(3):  # check each dimension independently
                if not face[i]:
                    continue
                # How much overlap you have with this dimension.
                d = (p[i] - np[i]) * face[i]
                if d < pad:
                    continue
                for dy in xrange(height):  # check each height
                    op = list(np)
                    op[1] -= dy
                    op[i] += face[i]
                    if tuple(op) not in self.model.world:
                        continue
                    p[i] -= (d - pad) * face[i]
                    if face == (0, -1, 0) or face == (0, 1, 0):
                        # You are colliding with the ground or ceiling, so stop
                        # falling / rising.
                        self.dy = 0
                    break
        return tuple(p)

    def on_mouse_press(self, x, y, button, modifiers):
        """ Called when a mouse button is pressed. See pyglet docs for button
        amd modifier mappings.

        Parameters
        ----------
        x, y : int
            The coordinates of the mouse click. Always center of the screen if
            the mouse is captured.
        button : int
            Number representing mouse button that was clicked. 1 = left button,
            4 = right button.
        modifiers : int
            Number representing any modifying keys that were pressed when the
            mouse button was clicked.

        """
        if self.exclusive:
            vector = self.get_sight_vector()
            block, previous = self.model.hit_test(self.position, vector)
            if (button == mouse.RIGHT) or \
                    ((button == mouse.LEFT) and (modifiers & key.MOD_CTRL)):
                # ON OSX, control + left click = right click.
                if previous:
                    #self.model.add_block(previous, self.player.block)
                    #self.player.selected.use(previous)
                    pass
            elif button == pyglet.window.mouse.LEFT and block:
                texture = self.model.world[block]
                if issubclass(self.player.selected.__class__, InventoryItem_Block):
                    if previous:
                        self.player.selected.use(previous)
                else:
                    if texture != BLOCKS["STONE"]:
                        self.player.selected.use(block)
        else:
            self.set_exclusive_mouse(True)

    def on_mouse_motion(self, x, y, dx, dy):
        """ Called when the player moves the mouse.

        Parameters
        ----------
        x, y : int
            The coordinates of the mouse click. Always center of the screen if
            the mouse is captured.
        dx, dy : float
            The movement of the mouse.

        """
        if self.exclusive:
            m = 0.15
            x, y = self.rotation
            x, y = x + dx * m, y + dy * m
            y = max(-90, min(90, y))
            self.rotation = (x, y)

    def on_key_press(self, symbol, modifiers):
        """ Called when the player presses a key. See pyglet docs for key
        mappings.

        Parameters
        ----------
        symbol : int
            Number representing the key that was pressed.
        modifiers : int
            Number representing any modifying keys that were pressed.

        """
        if symbol == key.W:
            self.strafe[0] -= 1
            CLIENT.send(dict(msg="action", action="player.move.forward.start", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        elif symbol == key.S:
            self.strafe[0] += 1
            CLIENT.send(dict(msg="action", action="player.move.backwards.start", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        elif symbol == key.A:
            self.strafe[1] -= 1
            CLIENT.send(dict(msg="action", action="player.move.left.start", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        elif symbol == key.D:
            self.strafe[1] += 1
            CLIENT.send(dict(msg="action", action="player.move.right.start", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        elif symbol == key.Q:
            self.player.selected.drop()
            CLIENT.send(dict(msg="action", action="player.selected.drop", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        elif symbol == key.E:
            CLIENT.send(dict(msg="action", action="player.pickup", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
            self.player.pickup()
        elif symbol == key.SPACE:
            if self.dy == 0:
                self.dy = JUMP_SPEED
                CLIENT.send(dict(msg="action", action="player.jump", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        elif symbol == key.ESCAPE:
            #self.set_exclusive_mouse(False)
            #CLIENT.send("client.disconnect")
            reactor.stop()
        elif symbol == key.TAB:
            self.flying = not self.flying
        elif symbol in self.num_keys:
            index = (symbol - self.num_keys[0]) % len(self.player.inventory.inventory)
            self.player.selected = self.player.inventory.inventory[index]
            self.UI.informItemKeyPressed(index)
        global ACTUAL_WALKING_SPEED
        if modifiers & key.LSHIFT:
            ACTUAL_WALKING_SPEED = 2
        else:
            ACTUAL_WALKING_SPEED = 5

    def on_key_release(self, symbol, modifiers):
        """ Called when the player releases a key. See pyglet docs for key
        mappings.

        Parameters
        ----------
        symbol : int
            Number representing the key that was pressed.
        modifiers : int
            Number representing any modifying keys that were pressed.

        """
        if symbol == key.W:
            self.strafe[0] += 1
            CLIENT.send(dict(msg="action", action="player.move.forward.stop", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        elif symbol == key.S:
            self.strafe[0] -= 1
            CLIENT.send(dict(msg="action", action="player.move.backwards.stop", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        elif symbol == key.A:
            self.strafe[1] += 1
            CLIENT.send(dict(msg="action", action="player.move.left.stop", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        elif symbol == key.D:
            self.strafe[1] -= 1
            CLIENT.send(dict(msg="action", action="player.move.right.stop", player_position=str(WINDOW.position), player_orientation=str(WINDOW.get_sight_vector())))
        global ACTUAL_WALKING_SPEED
        if modifiers & key.LSHIFT:
            ACTUAL_WALKING_SPEED = 2
        else:
            ACTUAL_WALKING_SPEED = 5

    def on_resize(self, width, height):
        """ Called when the window is resized to a new `width` and `height`.

        """
        # label
        self.label.y = height - 10
        # reticle
        if self.reticle:
            self.reticle.delete()
        x, y = self.width / 2, self.height / 2
        n = 10
        self.reticle = pyglet.graphics.vertex_list(4,
            ('v2i', (x - n, y, x + n, y, x, y - n, x, y + n))
        )

    def set_2d(self):
        """ Configure OpenGL to draw in 2d.

        """
        width, height = self.get_size()
        glDisable(GL_DEPTH_TEST)
        glViewport(0, 0, width, height)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, width, 0, height, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()

    def set_3d(self):
        """ Configure OpenGL to draw in 3d.

        """
        width, height = self.get_size()
        glEnable(GL_DEPTH_TEST)
        glViewport(0, 0, width, height)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        gluPerspective(65.0, width / float(height), 0.1, 60.0)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        x, y = self.rotation
        glRotatef(x, 0, 1, 0)
        glRotatef(-y, math.cos(math.radians(x)), 0, math.sin(math.radians(x)))
        x, y, z = self.position
        glTranslatef(-x, -y, -z)

    def on_draw(self):
        """ Called by pyglet to draw the canvas.

        """
        self.clear()
        self.set_3d()
        glColor3d(1, 1, 1)
        self.model.batch.draw()
        self.world_items.batch.draw()
        self.draw_focused_block()
        self.set_2d()
        self.draw_label()
        self.draw_reticle()

        # Enable alpha on objects (experimental)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
        glEnable( GL_BLEND );

        # Draw everything that's been added to the drawregister.
        for reg in self.drawregister.drawregister:
            reg()



    def draw_focused_block(self):
        """ Draw black edges around the block that is currently under the
        crosshairs.

        """
        vector = self.get_sight_vector()
        block = self.model.hit_test(self.position, vector)[0]
        if block:
            x, y, z = block
            vertex_data = cube_vertices(x, y, z, 0.51)
            glColor3d(0, 0, 0)
            glPolygonMode(GL_FRONT_AND_BACK, GL_LINE)
            pyglet.graphics.draw(24, GL_QUADS, ('v3f/static', vertex_data))
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)

    def draw_label(self):
        """ Draw the label in the top left of the screen.

        """
        x, y, z = self.position
        self.label.text = '%02d (%.2f, %.2f, %.2f) %d / %d' % (
            pyglet.clock.get_fps(), x, y, z,
            len(self.model._shown), len(self.model.world))
        self.label.draw()

    def draw_reticle(self):
        """ Draw the crosshairs in the center of the screen.

        """
        glColor3d(0, 0, 0)
        self.reticle.draw(GL_LINES)


def setup_fog():
    """ Configure the OpenGL fog properties.

    """
    # Enable fog. Fog "blends a fog color with each rasterized pixel fragment's
    # post-texturing color."
    glEnable(GL_FOG)
    # Set the fog color.
    glFogfv(GL_FOG_COLOR, (GLfloat * 4)(0.5, 0.69, 1.0, 1))
    # Say we have no preference between rendering speed and quality.
    glHint(GL_FOG_HINT, GL_DONT_CARE)
    # Specify the equation used to compute the blending factor.
    glFogi(GL_FOG_MODE, GL_LINEAR)
    # How close and far away fog starts and ends. The closer the start and end,
    # the denser the fog in the fog range.
    glFogf(GL_FOG_START, 20.0)
    glFogf(GL_FOG_END, 60.0)


def setup():
    """ Basic OpenGL configuration.

    """
    # Set the color of "clear", i.e. the sky, in rgba.
    glClearColor(0.5, 0.69, 1.0, 1)
    # Enable culling (not rendering) of back-facing facets -- facets that aren't
    # visible to you.
    glEnable(GL_CULL_FACE)
    # Set the texture minification/magnification function to GL_NEAREST (nearest
    # in Manhattan distance) to the specified texture coordinates. GL_NEAREST
    # "is generally faster than GL_LINEAR, but it can produce textured images
    # with sharper edges because the transition between texture elements is not
    # as smooth."
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
    setup_fog()


def main():
    global SERVER
    global CLIENT
    global STARTING_POSITION

    STARTING_POSITION = (0, 0, 0)
    LISTENSERVER = True

    CLIENTSERVER = MultiplayerClientServer()
    if LISTENSERVER == True:
        SERVER = MultiplayerServerServer()
        CLIENT = MultiplayerClientClient("localhost")
    else:
        if len(sys.argv) != 2:
            print "Invalid number of arguments. IP address of desired server must be provided as the only argument."
            return
        SERVER = False
        CLIENT = MultiplayerClientClient("server-ip-here")

    __builtin__.WINDOW = Window(width=800, height=600, caption='Pyglet', resizable=True)
    # Hide the mouse cursor and prevent the mouse from leaving the WINDOW.
    WINDOW.set_exclusive_mouse(True)
    setup()

    #CLIENT.send("HELLO!")
    reactor.run()

class MultiplayerClientClient:
    def __init__(self, addr):
        self.factory = pb.PBClientFactory()
        reactor.connectTCP(addr, 8770, self.factory)
        import socket
        j = dict(msg="init", addr=socket.gethostbyname(socket.gethostname()))
        self.send(j, False) # No UUID before connecting--UUID provided by server.
        #self.send('{ "msg": "init", "addr": "' + socket.gethostbyname(socket.gethostname()) + '" }')

    # include_uuid: Attempts to add (or modify existing) the uuid of the client to the request before sending.
    def send(self, msg, include_uuid=True):
        d = self.factory.getRootObject()
        if include_uuid == True:
            msg_fin = dict(msg)
            msg_fin.update(uuid=self.uuid)
        else:
            msg_fin = msg
        d.addCallback(lambda obj: obj.callRemote("receive", jsonpickle.encode(msg_fin, unpicklable=True)))
        #d.addCallback(lambda echo: "server echoed: " + echo)

class MultiplayerClientServer(pb.Root):
    def __init__(self):
        reactor.listenTCP(8771, pb.PBServerFactory(self))
    def remote_receive(self, pkt):
        j = jsonpickle.decode(pkt)

        if j[u'msg'] == "uuid":
            print "server provided uuid: " + j[u'uuid']
            global CLIENT
            CLIENT.uuid = j[u'uuid']
        elif j[u'msg'] == "player.position":
            print "server provided player.position: " + j[u'position']
            splt = j[u'position'].split(",")
            x = 0
            while x < 3:
                splt[x] = splt[x].replace("(", "").replace(")", "")
                x += 1
            WINDOW.position = (float(str(splt[0]).strip()), float(str(splt[1]).strip()), float(str(splt[2]).strip()))
        elif j[u'msg'] == "networkplayer.position":
            pass

# Get the distance between two three dimensional points (tuples).
def getDistance(xyz1, xyz2):
   return sqrt(math.pow(xyz1[0]-xyz2[0], 2) + math.pow(xyz1[1]-xyz2[1], 2) + math.pow(xyz1[2]-xyz2[2], 2))

class MultiplayerServerClient:
    def __init__(self, addr):
        self.factory = pb.PBClientFactory()
        reactor.connectTCP(addr, 8771, self.factory)
    def send(self, msg):
        d = self.factory.getRootObject()
        d.addCallback(lambda obj: obj.callRemote("receive", msg))

class MultiplayerServerServer(pb.Root):
    def __init__(self):
        self.clientList = []
        reactor.listenTCP(8770, pb.PBServerFactory(self))

    # Accepts a uuid (string) and attempts to find a client from the clientList.
    def getClient(self, uuid):
        for client in self.clientList:
            #print "PRINTING DEBUG DICT: " + jsonpickle.encode(client, unpicklable=True)
            if client[u'uuid'] == uuid:
                return client
        return False

    # Broadcast a packet consisting of a dict, which contains uuid of the broadcaster.
    # Optional c_op parameter accepts a function which gets called for each valid client found to broadcast to.
    #   The current found client is passed into c_op when called.
    def broadcastWithinRange(self, pkt, distance, c_op=False):
        for c in self.clientList:
            if(c[u'uuid'] != pkt[u'uuid']):
                if(getDistance(self.getClient(pkt.uuid), c.network_player.getPosition()) < distance):
                    if(c_op) != False:
                        c_op(c)
                    c.send(jsonpickle.encode(pkt))

    def remote_receive(self, pkt):
        j = jsonpickle.decode(pkt)

        if j[u'msg'] == "init":
            serverClient = MultiplayerServerClient(j[u'addr'])
            import uuid
            u = uuid.uuid4()
            global STARTING_POSITION
            np = NetworkPlayer(STARTING_POSITION)
            self.clientList.append(dict(uuid=str(u), server_client=serverClient, network_player=np))
            serverClient.send(jsonpickle.encode(dict(msg="uuid", uuid=str(u)), unpicklable=True))
            serverClient.send(jsonpickle.encode(dict(msg="player.position", position=str(np.getPosition())), unpicklable=True))
            self.broadcastWithinRange(dict(msg="networkplayer.position", uuid=j[u'uuid'], position=str(np.getPosition())), 10)

        elif j[u'msg'] == "action":
            print "SERVER ACTION: " + j[u'action']

            np = self.getClient(j[u'uuid'])[u'network_player']

            if j[u'action'] == "player.move.forward.start":
                def op(client):
                    client.network_player.strafe[0] -= 1
                self.broadcastWithinRange(dict(msg=j[u'action'], uuid=j[u'uuid'], position=str(np.getPosition())), 10, op)
            elif j[u'action'] == "player.move.forward.stop":
                def op(client):
                    client.network_player.strafe[0] += 1
                self.broadcastWithinRange(dict(msg=j[u'action'], uuid=j[u'uuid'], position=str(np.getPosition())), 10, op)
            elif j[u'action'] == "player.move.backwards.start":
                def op(client):
                    client.network_player.strafe[0] += 1
                self.broadcastWithinRange(dict(msg=j[u'action'], uuid=j[u'uuid'], position=str(np.getPosition())), 10, op)
            elif j[u'action'] == "player.move.backwards.stop":
                def op(client):
                    client.network_player.strafe[0] -= 1
                self.broadcastWithinRange(dict(msg=j[u'action'], uuid=j[u'uuid'], position=str(np.getPosition())), 10, op)
            elif j[u'action'] == "player.move.left.start":
                def op(client):
                    client.network_player.strafe[1] -= 1
                self.broadcastWithinRange(dict(msg=j[u'action'], uuid=j[u'uuid'], position=str(np.getPosition())), 10, op)
            elif j[u'action'] == "player.move.left.stop":
                def op(client):
                    client.network_player.strafe[1] += 1
                self.broadcastWithinRange(dict(msg=j[u'action'], uuid=j[u'uuid'], position=str(np.getPosition())), 10, op)
            elif j[u'action'] == "player.move.right.start":
                def op(client):
                    client.network_player.strafe[1] += 1
                self.broadcastWithinRange(dict(msg=j[u'action'], uuid=j[u'uuid'], position=str(np.getPosition())), 10, op)
            elif j[u'action'] == "player.move.right.stop":
                def op(client):
                    client.network_player.strafe[1] -= 1
                self.broadcastWithinRange(dict(msg=j[u'action'], uuid=j[u'uuid'], position=str(np.getPosition())), 10, op)


'''
class RegisterUser(Command):
    arguments = [('test', String())]
    response = [('t', String())]

class Test(Protocol):
    def dataReceived(self, data):
        self.transport.write("got it! " + data)
class TestFactory(Factory):
    def buildProtocol(self, addr):
        return Test()
'''
'''
class MultiplayerClient:
    def __init__(self):
        self.connection = self.connect()
        #self.connection.addCallback(self.connected)
        ##self.connection.addErrback(err)
    def connect(self):
        endpoint = TCP4ClientEndpoint(reactor, "127.0.0.1", 8750)
        factory = Factory()
        #factory.protocol = AMP
        return endpoint.connect(factory)
    def connected(self, protocol):
        #x = protocol.callRemote(RegisterUser, test="testuser")
        #x = protocol.transport.write("testmessage")
        #return x
        pass

class MultiplayerServer:
    def __init__(self):
        self.connection = self.connect()
        #self.connection.addCallback(self.connected)
        self.connection.addErrback(err)
    def connect(self):
        endpoint = TCP4ServerEndpoint(reactor, 8750)
        factory = Factory()
        factory.protocol = AMP
        return endpoint.listen(factory)
    def remote_RegisterUser(self, arg):
        print "RegisterUser: " + arg
'''
if __name__ == '__main__':
    main()
