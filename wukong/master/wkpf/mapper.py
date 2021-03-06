import sys, os, traceback, copy
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from parser import *
from locationTree import *
from xml.dom.minidom import parse, parseString
from xml.parsers.expat import ExpatError
import simplejson as json
import logging, logging.handlers, wukonghandler
from collections import namedtuple
from locationParser import *
from codegen import CodeGen
from xml2java.generator import Generator
import copy
from threading import Thread
import traceback
import time
import re
import StringIO
import shutil, errno
import datetime
from subprocess import Popen, PIPE, STDOUT

from configuration import *
from globals import *

# allcandidates are all node ids ([int])
def constructHeartbeatGroups(heartbeatgroups, routingTable, allcandidates):
  del heartbeatgroups[:]

  while len(allcandidates) > 0:
    heartbeatgroup = namedtuple('heartbeatgroup', ['nodes', 'period'])
    heartbeatgroup.nodes = []
    heartbeatgroup.period = 1
    if len(heartbeatgroup.nodes) == 0:
      heartbeatgroup.nodes.append(allcandidates.pop(0)) # should be random?
      pivot = heartbeatgroup.nodes[0]
      if pivot.id in routingTable:
        for neighbor in routingTable[pivot.id]:
          neighbor = int(neighbor)
          if neighbor in [x.id for x in allcandidates]:
            for candidate in allcandidates:
              if candidate.id == neighbor:
                heartbeatgroup.nodes.append(candidate)
                allcandidates.remove(candidate)
                break
      heartbeatgroups.append(heartbeatgroup)

# assign periods
def determinePeriodForHeartbeatGroups(components, heartbeatgroups):
  for component in components:
    for wuobject in component.instances:
      for group in heartbeatgroups:
        if wuobject.node_id in [x.id for x in group.nodes]:
          #group heartbeat is reactiontime divided by 2, then multiplied by 1000 to microseconds
          newperiod = int(float(component.reaction_time) / 2.0 * 1000.0)
          if not group.period or (group.period and group.period > newperiod):
            group.period = newperiod
          break

def sortCandidates(wuObjects):
    nodeScores = {}
    for candidates in wuObjects:
      for node in candidates:
        if node[0] in nodeScores:
          nodeScores[node[0]] += 1
        else:
          nodeScores[node[0]] = 1

    for candidates in wuObjects:
      sorted(candidates, key=lambda node: nodeScores[node[0]], reverse=True)


# Filling changesets with wuobjects, and assign new changesets to last_changesets
# Return commands (a diff between changesets and last_changesets)
def first_of(changesets, network_info, last_changesets):

  if not changesets:
    return None, last_changesets

  if len(changesets.components) != len(last_changesets.components):
    raise Exception("[ERROR] components size doesn't match")

  l_type = lambda c: c.type
  if set(map(l_type, changesets.components)) != set(map(l_type,
    last_changesets.components)):
    raise Exception("[ERROR] components doesn't match")

  for component in changesets.components:
    locationquery = component.location
    candidatesize = component.group_size

    if candidatesize == 0:
      continue

    # filter by location
    if not locationquery in network_info:
      print '[WARNING] No nodes in this region', locationquery, 'for component', component.type, 'skipping...'
      continue

    candidates = network_info[locationquery] # WuNodes

    if len(candidates) < candidatesize:
      print '[WARNING] There is not enough nodes/candidates for component %s in region %s' % (component.type, locationquery)

    candidates = sorted(candidates, key=lambda candidate:
        candidate.energy, reverse=True)

    # Limit to candidatesize
    candidates = candidates[:candidatesize]

    wuobjects = []
    for node in candidates:
      wuobjects += node.wuobjects()

    component.instances = wuobjects
    
    if len(component.instances) == 0:
      raise Exception('[ERROR] No avilable match could be found for component %s' % (component))

  #print 'mapped application', changesets

  # Commands
  # format:
  # [
  #   wuproperty object
  #   ...
  # ]
  commands = []
  a_list = sorted(last_changesets.components, key=lambda x: x.location)
  b_list = sorted(changesets.components, key=lambda x: x.location)
  zipped = zip(a_list, b_list)
  for pair in zipped:
    print pair
    region_before = set([x.identity for x in pair[0].instances])
    region_after = set([x.identity for x in pair[1].instances])

    # First case, turning state on
    on_identities = region_after - region_before
    print 'on', on_identities
    for identity in on_identities:
      #wuclass = WuClass.find(identity=identity)
      #if not wuclass:
        #raise Exception('invalid wuclass identity' % (identity))
      wuobject = WuObject.find(identity=identity)
      if not wuobject:
        raise Exception('no wuobject for wuclass idenity' % (identity))
      # Getting the first property, assuming property number is 0
      # And assuming the property we get has datatype 'boolean'
      #wupropertydef = WuPropertyDef.find(number=0, wuclass_id=wuclass.wuclassdef().id)
      #wuproperty = WuProperty.find(wuobject_identity=wuobject.identity,
          #wupropertydef_identity=wupropertydef.identity)
      wuproperty = wuobject.wuproperties()[0]
      wuproperty.value = True
      print 'property', wuproperty
      print 'property node', wuproperty.wuobject().wunode()
      commands.append(wuproperty)

    region_before = set([x.identity for x in pair[0].instances])
    region_after = set([x.identity for x in pair[1].instances])

    # Second case, turning state off
    off_identities = region_before - region_after
    print 'off', off_identities
    for identity in off_identities:
      #wuclass = WuClass.find(identity=identity)
      #if not wuclass:
        #raise Exception('invalid wuclass identity' % (identity))
      wuobject = WuObject.find(wuclass_identity=identity)
      # Getting the first property, assuming property number is 0
      # And assuming the property we get has datatype 'boolean'
      #wupropertydef = WuPropertyDef.find(number=0, wuclass_id=wuclass.wuclassdef().id)
      #wuproperty = WuProperty.find(wuobject_identity=wuobject.identity,
          #wupropertydef_identity=wupropertydef.identity)
      wuproperty = wuobject.wuproperties()[0]
      wuproperty.value = False
      print 'property', wuproperty
      print 'property node', wuproperty.wuobject().wunode()
      commands.append(wuproperty)

  return commands, changesets



def firstCandidate(logger, changesets, routingTable, locTree):
    #input: nodes, WuObjects, WuLinks, WuClassDefsm, wuObjects is a list of wuobject list corresponding to group mapping
    #output: assign node id to WuObjects
    # TODO: mapping results for generating the appropriate instiantiation for different nodes

    # construct and filter candidates for every component on the FBP (could be the same wuclass but with different policy)
    for component in changesets.components:
        locationquery = component.location
        mincandidates = component.group_size

        # filter by location
        locParser = LocationParser(locTree)
        try:
            tmpSet = locParser.parse(locationquery)
        except:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            logger.error('cannot find match for location query %s, so we will invalid the query and pick all by default', locationquery)
            logger.error('Locality conditions for component wuclass id "%s" are too strict; no available candidate found' % (wuObject[0].getWuClass().getId()))
            logging.error("location Tree")
            logging.error(locTree.printTree())
            set_wukong_status(str(exc_type)+str(exc_value))
            tmpSet = locTree.getAllAliveNodeIds()
        candidates = tmpSet


        if len(candidates) < mincandidates:
            msg = '[WARNING] there is no enough candidates %r for component %s, but mapper will continue to map' % (candidates, component)
            set_wukong_status(msg)
            logger.warning(msg)

        # This is really tricky, basically there are two things you have to understand to understand this code
        # 1. There are wuclass that are only for reference and other for actually having a c function on the node
        # 2. All WuObjects have a wuclass, but it lies either in one of those types mentioned in #1
        # 3. Usually we assume 'hard' wuclasses will have only native wuclass on the node, so the there will be no wuobjects with a wuclass not in the node.wuclasses list
        # 4. node.wuclasses contains wuclasses that the node have code for it
        # 5. node.wuobjects contains just wuobjects from any wuclasses, some might based on wuclass in node.wuclasses list
        # e.g. A threahold wuclass would have native implementation (also in node.wuclasses) or a virtual implementation (not in node.wuclasses)

        # construct wuobjects, instances of component
        for candidate in candidates:
            node = locTree.getNodeInfoById(candidate)
            wuclassdef = WuClassDef.find(name=component.type)
            if wuclassdef.type.lower() == 'hard':
                # only native implementation
                if wuclassdef.id in [x.wuclassdef().id for x in node.wuclasses()]:
                    # has wuclass for native implementation
                    if wuclassdef.id in [x.wuclass().wuclassdef().id for x in node.wuobjects()]:
                        # use existing wuobject
                        for wuobject in node.wuobjects():
                            if wuobject.wuclass().wuclassdef().id == wuclassdef.id:
                                component.instances.append(wuobject)
                                break
                    else:
                        # create a new wuobject
                        sensorNode = locTree.sensor_dict[node.id]
                        sensorNode.initPortList(forceInit = False)
                        port_number = sensorNode.reserveNextPort()
                        for wuclass in node.wuclasses():
                            if wuclass.wuclassdef().id == wuclassdef.id:
                                wuobject = WuObject.create(port_number, wuclass)
                        wuobject.save()
                        component.instances.append(wuobject)
            else:
                # prefer native implementation
                if wuclassdef.id in [x.wuclassdef().id for x in node.wuclasses()]:
                    # has wuclass for native implementation
                    if wuclassdef.id in [x.wuclass().wuclassdef().id for x in node.wuobjects()]:
                        # use existing wuobject
                        for wuobject in node.wuobjects():
                            if wuobject.wuclass().wuclassdef().id == wuclassdef.id:
                                wuobject.save()
                                component.instances.append(wuobject)
                                break
                    else:
                        # create a new wuobject
                        sensorNode = locTree.sensor_dict[node.id]
                        sensorNode.initPortList(forceInit = False)
                        port_number = sensorNode.reserveNextPort()
                        for wuclass in node.wuclasses():
                            if wuclass.wuclassdef().id == wuclassdef.id:
                                wuobject = WuObject.create(port_number, wuclass)
                        wuobject.save()
                        component.instances.append(wuobject)
                else:
                    # no wuclass for native implementation
                    if wuclassdef.id in [x.wuclass().wuclassdef().id for x in node.wuobjects()]:
                        # use existing virtual wuobject
                        for wuobject in node.wuobjects():
                            if wuobject.wuclass().wuclassdef().id == wuclassdef.id:
                                wuobject.save()
                                component.instances.append(wuobject)
                                break
                    else:
                        # create a new virtual wuobject
                        sensorNode = locTree.sensor_dict[node.id]
                        sensorNode.initPortList(forceInit = False)
                        port_number = sensorNode.reserveNextPort()
                        wuclass = WuClass.find(wuclassdef_identity=wuclassdef.identity)
                        if not wuclass:
                          wuclass = WuClass.create(wuclassdef, node, True)
                        wuobject = WuObject.create(port_number, wuclass)
                        wuobject.save()
                        component.instances.append(wuobject)

        def prefer_hard(wuobject):
            return not wuobject.wuclass().virtual

        component.instances = sorted(component.instances, key=prefer_hard, reverse=True)
        
        if len(component.instances) == 0:
          logger.error ('[ERROR] No avilable match could be found for component %s' % (component))
          return False

    # sort candidates
    # TODO:simple sorting, first fit, last fit, hardware fit, etc
    #sortCandidates(changesets.components)

    # limit to min candidate if possible
    component.instances = component.instances[:mincandidates]

    # construct heartbeat groups plus period assignment
    allcandidates = set()
    for component in changesets.components:
        for wuobject in component.instances:
            allcandidates.add(wuobject.wunode().id)
    allcandidates = list(allcandidates)
    allcandidates = map(lambda x: WuNode.find(id=x), allcandidates)
    # constructHeartbeatGroups(changesets.heartbeatgroups, routingTable, allcandidates)
    # determinePeriodForHeartbeatGroups(changesets.components, changesets.heartbeatgroups)
    logging.info('heartbeatGroups constructed, periods assigned')
    logging.info(changesets.heartbeatgroups)

    #delete and roll back all reservation during mapping after mapping is done, next mapping will overwritten the current one
    for component in changesets.components:
        for wuobj in component.instances:
            senNd = locTree.getSensorById(wuobj.wunode().id)
            for j in senNd.temp_port_list:
                senNd.port_list.remove(j)
            senNd.temp_port_list = []
    return True
