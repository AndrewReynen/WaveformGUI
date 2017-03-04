# -*- coding: utf-8 -*-
# Version 0.1.0
# Copyright Andrew.M.G.Reynen
import sys
import logging
import sip
import os
import numpy as np
sip.setapi('QVariant', 2)
sip.setapi('QString', 2)
from PyQt4 import QtGui,QtCore
from PyQt4.QtCore import QSettings
from MainWindow import Ui_MainWindow
from CustomWidgets import TraceWidget, keyPressToString
from CustomFunctions import getTimeFromFileName
from HotVariables import initHotVar
from Preferences import defaultPreferences, Pref, DateDialog
from Actions import defaultActions, defaultPassiveOrder
from Archive import getArchiveAvail, extractDataFromArchive
from ConfigurationDialog import ConfDialog
from SaveSource import CsDialog
from fnmatch import fnmatch
from future.utils import iteritems
from pyqtgraph import mkPen,mkBrush

# Main window class
class LazylystMain(QtGui.QMainWindow, Ui_MainWindow):
    def __init__(self):
        QtGui.QMainWindow.__init__(self)
        Ui_MainWindow.__init__(self)
        self.setupUi(self)
        self.loadSettings()
        self.applyPreferences()
        self.setFunctionality()
        self.introduction()
    
    # Go through all preferences, and call their update functions
    def applyPreferences(self):
        # The pick color preferences are generated here if not already present...
        # ...so iterate through static version of prefs
        curPrefs=[aPref for key, aPref in iteritems(self.pref)]
        for aPref in curPrefs:
            aPref.update(self,init=True)
        
    # Start setting up some functionality to the UI
    def setFunctionality(self):
        # Connect the zoomed in and zoomed out views of the event files
        self.archiveSpan.span.sigRegionChanged.connect(lambda: self.archiveEvent.updateXRange(self.archiveSpan.span))
        self.archiveSpan.span.sigRegionChanged.connect(self.updateSpanText)
        self.archiveSpan.span.sigRegionChangeFinished.connect(self.updateArchiveSpanList)
        # Allow user to set manually the ranges (no need to use slider)
        self.archiveSpanT0Label.doubleClicked.connect(lambda: self.setSpanBoundViaDialog('T0'))
        self.archiveSpanT1Label.doubleClicked.connect(lambda: self.setSpanBoundViaDialog('T1'))
        # Give ability to the archive displays
        self.archiveList.graph=self.archiveEvent
        self.archiveList.graph.addNewEventSignal.connect(self.addPickFile)
        self.archiveList.clicked.connect(self.setFocus) # Do not let it steal focus from keybinds
        self.archiveList.doubleClicked.connect(self.archiveListDoubleClickEvent)
        # Give ability to the map
        self.mapWidget.staDblClicked.connect(self.mapDoubleClickEvent)
        # Allow stdout to be sent to the Trace Log
        XStream.stdout().messageWritten.connect(self.textOutBrowser.insertPlainText)
        XStream.stderr().messageWritten.connect(self.textOutBrowser.insertPlainText)
        self.textOutBrowser.textChanged.connect(self.scrollTextOut)
        
    # Scroll the trace log to the bottom if an error occurs
    def scrollTextOut(self):
        scrollBar=self.textOutBrowser.verticalScrollBar()
        scrollBar.setValue(scrollBar.maximum())
    
    # Report the current keybinds for the configuration and change source actions
    def introduction(self):
        print('Press: '+self.act['ChangeSource'].trigger.toString()+' to change source (archive/picks/station metadata)')
        print('Press: '+self.act['OpenOptions'].trigger.toString()+' to open the configuration dialog')
    
    # Function to handle key board input from user
    def keyPressEvent(self, ev):
        super(LazylystMain, self).keyPressEvent(ev)
        keyname=keyPressToString(ev)
        if keyname==None:
            return
        # Loop through all actions and see if one is activated...
        # ... use the original set (an action may be added part way through, so can mess with the loop)
        actions=[action for key,action in iteritems(self.act) if action.trigger!='DoubleClick']
        for action in actions:
            if not action.passive and action.trigger.toString()==keyname:
                self.processAction(action)

    # Function to handle double click events from trace widgets
    def traceDoubleClickEvent(self):
        action=self.act['PickAdd']
        self.processAction(action)
    
    # Function to handle double clicks from the archive widget
    def archiveListDoubleClickEvent(self):
        action=self.act['PickFileSetToClick']
        self.processAction(action)
    
    # Function to handle double clicks from the map widget
    def mapDoubleClickEvent(self):
        action=self.act['MapStaDblClicked']
        self.processAction(action)
    
    # Create and activate the queue of actions following the initiation of an active action
    def processAction(self,action):
        # If this action is timed...
        if action.timer:
            # ... see if already being run, and stop if so
            curTimers=self.qTimers.keys()
            if action.tag in curTimers:
                self.stopTimedAction(action)
            # ... otherwise add to the timers, and kick it off
            else:
                timer = QtCore.QTimer()
                timer.timeout.connect(lambda: self.runActiveAction(action))
                self.qTimers[action.tag]=timer
                self.qTimers[action.tag].start(action.timerInterval*1000.0)
                self.updateStrollingList()
        # If not timed, just do it once
        else:
            self.runActiveAction(action)

    # Run the specified action
    def runActiveAction(self,action):
        # Set the current station, and current timeRange to be sent to actions
        self.setCurTraceSta()
        self.setTimeRange()
        # First check to see if there are any (passive) actions which relate
        actQueue=self.collectActQueue(action)   
        for oAct in actQueue:
            self.executeAction(oAct)
                
    # Get the appropriate order of passive functions before and after their triggered active action
    def collectActQueue(self,action):
        beforeActive=[] # Passive actions with beforeTrigger
        afterActive=[] # Passive actions without beforeTrigger
        # Collect all the passive actions which have this active action as a trigger...
        # ...in the order specified within the configuration list
        for actTag in self.actPassiveOrder:
            if action.tag not in self.act[actTag].trigger:
                continue
            if self.act[actTag].beforeTrigger:
                beforeActive.append(self.act[actTag])
            else:
                afterActive.append(self.act[actTag])
        return beforeActive+[action]+afterActive

    # Function to handle the execution of an action already queued
    def executeAction(self,action):
        # If the actions function wasn't initialized, skip
        if action.func==None:
            return
        # Collect the required inputs
        inputs=[]
        hotVarKeys=[aKey for aKey in self.hotVar.keys()]
        for key in action.inputs:
            if key=='stream':
                inputs.append(self.hotVar[key].val.copy())
            elif key in hotVarKeys:
                inputs.append(self.hotVar[key].getVal())
            else:
                inputs.append(self.pref[key].getVal())
        # Call the function with args and kwargs
        returnVals=action.func(*inputs,**action.optionals)
        # Update all return (hot variable) values...
        # ... if no returns, but got something, let user know
        if len(action.returns)==0:
            if str(returnVals)!=str(None):
                print('Action '+action.tag+' expected no returns, but received some')
            return
        # ...if just one return, convert to list to treat the same as multiple
        elif len(action.returns)==1:
            returnVals=[returnVals]
        # Go through each of the returns in order, and update...
        # ...check first to see the number of returns are correct
        # ...and that the previous/new types match
        # ...pass (ignore) updates on variables with "$pass" as the return value
        if len(action.returns)==len(returnVals):
            skipUpdates=False
            # Ensure all the variable types match before updating any hot variables...
            # ...also ensure that the hot variables ("sanity") check function passes
            for i,aReturnKey in enumerate(action.returns):
                # Do not do any checks if the user wanted to pass this returns value
                if str(returnVals[i])=='$pass':
                    continue
                if not self.testReturnType(self.hotVar[aReturnKey].dataType,type(returnVals[i])):
                    print('Action '+action.tag+' expected variable '+str(self.hotVar[aReturnKey].dataType)+
                           ' for hot variable '+aReturnKey+', got '+str(type(returnVals[i])))
                    skipUpdates=True
                    # If the type is wrong, no point in the longer check
                    continue
                # Test if the return variable passes the built in check
                if self.hotVar[aReturnKey].check==None:
                    continue
                elif not self.hotVar[aReturnKey].check(self,returnVals[i]):
                    print('Action '+action.tag+' failed '+aReturnKey+' check')
                    skipUpdates=True
            if skipUpdates: 
                # Stop the action if it is timed
                if action.timer:
                    self.stopTimedAction(action)
                return
            # Process the return keys in order ...
            for i,aReturnKey in enumerate(action.returns):
                # ... passing over any which have $pass as the return value
                if str(returnVals[i])=='$pass':
                    pass
                else:
                    self.hotVar[aReturnKey].val=returnVals[i]
                    self.hotVar[aReturnKey].update()
        else:
            print('For action '+action.tag+' got '+str(len(returnVals))+
                   ' return values, expected '+str(len(action.returns)))
    
    # As some variable types are quite similar, allow some to be treated the same...
    # ... for now just str and np.string_
    def testReturnType(self,wantType,returnType):
        # Accepted variations for strings
        if wantType==str and returnType in [str,np.string_]:
            return True
        # All other data types
        elif wantType==returnType:
            return True
        return False
            
            
    # Remove a timed action from the queue
    def stopTimedAction(self,action):
        self.qTimers[action.tag].stop()
        self.qTimers.pop(action.tag)
        self.updateStrollingList()
    
    # Open the configuration window
    def openConfiguration(self):
        self.dialog=ConfDialog(actions=self.act,main=self,
                               pref=self.pref,hotVar=self.hotVar)
        self.dialog.exec_()
        
    # Open the change source window
    def openChangeSource(self):
        self.dialog=CsDialog(self.hotVar,self.saveSource)
        # Extract the wanted source
        if self.dialog.exec_():
            self.hotVar['sourceTag'].val=self.dialog.returnSource().tag
            self.hotVar['sourceTag'].update()
    
    # Update the source to a specific source tag
    def updateSource(self):
        source=self.saveSource[self.hotVar['sourceTag'].val]
        # If the files exist, update the hot variables
        if source.pathExist():
            for key,val in [['archDir',source.archDir],
                            ['pickDir',source.pickDir],
                            ['staFile',source.staFile]]:
                self.hotVar[key].val=val
                self.hotVar[key].update()
            ## Reset some source specific hot variables ##
        else:
            print('Source update skipped')

    # Add an empty pick file to the pick directory, also add to GUI list
    def addPickFile(self):
        # Ignore if no archive is currently set
        if self.hotVar['archDir'].val=='':
            return
        aTimeStr=self.archiveList.graph.newEveStr
        # Assign an ID to the new event
        seenIDs=[int(aFile.split('_')[0]) for aFile in self.hotVar['pickFiles'].val]
        # Use the specified ID generation style
        if self.pref['eveIdGenStyle'].val=='fill':
            newID=0
            while newID in seenIDs:
                newID+=1
        elif self.pref['eveIdGenStyle'].val=='next':
            if len(seenIDs)==0:
                newID=0
            else:
                newID=np.max(seenIDs)+1
        else:
            print('The eveIDGenStyle '+self.pref['eveIdGenStyle'].val+' has not been implemented')
            return
        # Use the previous file ID length, default at 10 if no files present
        zFillLen=10
        if len(seenIDs)>0:
            zFillLen=len(self.hotVar['pickFiles'].val[0].split('_')[0])
        newPickFile=str(newID).zfill(zFillLen)+'_'+aTimeStr+'.picks'
        # Add to the picks directory
        newFile=open(self.hotVar['pickDir'].val+'/'+newPickFile,'w')
        newFile.close()
        # Add to GUI list, and internal list
        self.hotVar['pickFiles'].val=np.sort(os.listdir(self.hotVar['pickDir'].val))
        self.updateEveSort()
        self.archiveEvent.updateEveLines(self.hotVar['pickFileTimes'].val,self.hotVar['curPickFile'].val,
                                         self.pref['archiveColorEve'].val,self.pref['archiveColorSelect'].val)
    
    # Load a specific pick file from the pick directory (inter-event pick loading)
    def loadPickFile(self):
        path=self.hotVar['pickDir'].val+'/'+self.hotVar['curPickFile'].val
        # Check that the path exists
        if not os.path.exists(path):
            print('Pick file at '+path+' no longer exists')
            return
        # Check that the file has some content
        elif os.path.getsize(path)==0:
            return
        pickSet=np.genfromtxt(path,delimiter=',',dtype=str)
        # Put into proper dimensions if only one pick was present
        if len(pickSet.shape)==1:
            pickSet=pickSet.reshape((1,3))
        pickSet=self.remUnknownPickTypes(pickSet)
        # Update the pickset
        self.hotVar['pickSet'].val=pickSet
        
    # Save the current picks
    def savePickSet(self):
        # If the current pick file was not yet initiated, nothing to save
        if self.hotVar['curPickFile'].val=='':
            return
        np.savetxt(self.hotVar['pickDir'].val+'/'+self.hotVar['curPickFile'].val,
                   self.hotVar['pickSet'].val,fmt='%s',delimiter=',')
    
    # Remove any lines which have pick types that are not currently defined
    def remUnknownPickTypes(self,pickSet):
        knownTypes=[key for key in self.pref['pickTypesMaxCountPerSta'].val]
        seenTypes=np.unique(pickSet[:,1])
        for aType in seenTypes:
            if aType not in knownTypes:
                print('Pick type: '+aType+' is not currently defined in preference pickTypesMaxCountPerSta'+
                       ' and has been removed from the hot variable pickSet')
                pickSet=pickSet[np.where(pickSet[:,1]!=aType)]
        return pickSet
        
    # Set the current pick file, called from double click an event in the archive list
    def setCurPickFileOnClick(self):
        return str(self.archiveList.currentItem().text())
        
    # Load the event related information
    def updateEvent(self):
        # Clear away the previous set of picks
        self.hotVar['pickSet'].val=np.empty((0,3))
        # Go back to the first page
        self.hotVar['curPage'].val=0
        # Set the title of the event
        self.timeWidget.getPlotItem().setLabels(title=self.hotVar['curPickFile'].val)
        # Load the picks from given pick file, if it has been initialized...
        if self.hotVar['curPickFile'].val!='':
            self.loadPickFile()
            # Ensure that the archive span and list includes this file
            self.checkArchiveSpan()
            # Get the wanted time, and query for the data
            aTime=getTimeFromFileName(self.hotVar['curPickFile'].val).timestamp
            t1,t2=aTime+self.pref['evePreTime'].val,aTime+self.pref['evePostTime'].val
            self.hotVar['stream'].val=extractDataFromArchive(t1,t2,self.hotVar['archFiles'].val,
                                                             self.hotVar['archFileTimes'].val)
            # Get the trace background coloring
            self.traceBgColors=self.getStaColors('traceBg')
            # Make a copy for any filtering to be applied
            self.hotVar['pltSt'].val=self.hotVar['stream'].val.copy()
            # Sort traces by channel so they are added in same order (relative other stations)
            self.hotVar['pltSt'].val.sort(keys=['channel'])
            # Alphabetical sorting by default
            self.hotVar['staSort'].val=np.sort(np.unique([str(tr.stats.station) for tr in self.hotVar['stream'].val]))
            # If there is data...
            # ...move the axis time limit to the appropriate position
            if len(self.hotVar['stream'].val)!=0:
                minTime=np.min([tr.stats.starttime.timestamp for tr in self.hotVar['stream'].val])
                maxTime=np.max([tr.stats.endtime.timestamp for tr in self.hotVar['stream'].val])
                self.timeWidget.plotItem.setXRange(minTime,maxTime)
            # ...let user know otherwise
            else:
                print('No data extracted from archive for curPickFile')
        # ...Otherwise load the default hot variables values, and reset values relating to curPickFile
        else:
            defaultHot=initHotVar()
            for key in ['stream','pltSt','staSort','pickSet','curTraceSta']:
                self.hotVar[key].val=defaultHot[key].val
        # Add data, and picks to the station widgets
        self.updatePage()
        # Update the highlighted event on the archive visual
        self.archiveEvent.updateEveLineSelect(self.hotVar['curPickFile'].val,self.pref['archiveColorSelect'].val)
    
    # Update the data and picks on the current page
    def updatePage(self,init=False):
        # Update the trace curves
        self.updateTraces()
        # Update the picks
        self.updatePagePicks()
    
    # Function to handle updates of the hot variable curPage
    def updateCurPage(self):
        maxPageNum=(len(self.hotVar['staSort'].val)-1)/self.pref['staPerPage'].val
        if maxPageNum==-1:
            return
        # Clip to the max/minimum if outside the accepted range
        if self.hotVar['curPage'].val>maxPageNum:
            self.hotVar['curPage'].val=maxPageNum
        elif self.hotVar['curPage'].val<0:
            self.hotVar['curPage'].val=0
        self.updatePage()
    
    # Update the time range upon user request
    def updateTimeRange(self):
        t0,t1=self.hotVar['timeRange'].val
        self.timeWidget.setXRange(t0,t1,padding=0.0)
        
    # Built in function to tab to the next or previous page number
    def tabCurPage(self,nextPage=False,prevPage=False,pageNum=0):
        maxPageNum=(len(self.hotVar['staSort'].val)-1)/self.pref['staPerPage'].val
        # If the next or previous page, ensure still in bounds
        if nextPage and self.hotVar['curPage'].val+1<=maxPageNum:
            self.hotVar['curPage'].val+=1
        elif prevPage and self.hotVar['curPage'].val-1>=0:
            self.hotVar['curPage'].val-=1
        else:
            return
        # If got to the end, the page number must have changed, update the page
        self.updatePage()
    
    # If the current pick file moved outside of the selected range...
    # ...increase the range to include the pick file
    def checkArchiveSpan(self):
        fileTime=getTimeFromFileName(self.hotVar['curPickFile'].val).timestamp
        t1,t2=self.archiveSpan.span.getRegion()
        reset=False
        if fileTime<t1:
            t1=fileTime-0.02*(t2-t1)
            reset=True
        if fileTime>t2:
            t2=fileTime+0.02*(t2-t1)
            reset=True
        # Move the span select, and the archive time limits
        if reset:
            self.archiveSpan.span.setRegion((t1,t2))
            self.archiveSpan.setXRange(t1,t2) # Padding is already included here
        
    # Set which station is currently being hovered over
    def setCurTraceSta(self):
        for widget in self.staWidgets:
            if widget.hasFocus():
                self.hotVar['curTraceSta'].val=widget.sta
                return
        self.hotVar['curTraceSta'].val=''
    
    # Set the timeRange variable to the current time range of the time widget
    def setTimeRange(self):
        self.hotVar['timeRange'].val=self.timeWidget.getTimeRange()
        
    # Add a pick to the double-clicked station (single-pick addition)
    def addClickPick(self):
        if self.hotVar['curPickFile'].val=='':
            return
        # Return if no pick mode selected
        if self.hotVar['pickMode'].val=='':
            print('No pick mode selected')
            return
        # Figure out which widget was picked on
        aList=[aWidget for aWidget in self.staWidgets if aWidget.hasFocus()]
        if len(aList)!=1:
            print('Picking is out of focus, skipped')
            return
        widget=aList[0]
        curMode=self.hotVar['pickMode'].val
        # Append this pick to the pick set, and plotted lines
        self.hotVar['pickSet'].val=np.vstack((self.hotVar['pickSet'].val,
                                              [widget.sta,curMode,widget.clickPos]))
        widget.addPick(widget.clickPos,curMode,self.getPickPen(curMode))
        # Remove picks from the plot and pickSet, where there are too many
        self.remExcessPicks(checkStas=[widget.sta],checkTypes=[curMode])

    # Remove older picks, which occurs when a given pick type has more than "X"...
    # ...number of picks on a given station (see pref['pickTypesMaxCountPerSta'])
    def remExcessPicks(self,checkStas=None,checkTypes=None):
        # If sta or types is present, check only to delete those pickTypes on given stations
        if checkStas==None:
            checkStas=self.hotVar['staSort'].val
        if checkTypes==None:
            checkTypes=[key for key in self.pref['pickTypesMaxCountPerSta']]
        delIdxs=[]
        visStas=[widget.sta for widget in self.staWidgets]
        # Gather all indicies to be removed from pickSet and pltLines
        for pickType in checkTypes:
            for sta in checkStas:
                potIdxs=np.where((self.hotVar['pickSet'].val[:,0]==sta)&
                                 (self.hotVar['pickSet'].val[:,1]==pickType))[0]
                pickCountMax=self.pref['pickTypesMaxCountPerSta'].val[pickType]
                # If there are more picks than allowed, add to delete indices
                if len(potIdxs)>pickCountMax:
                    delIdxs+=list(np.sort(potIdxs)[:-pickCountMax])
                    # Delete the excess lines from the widget (if currently shown)
                    if sta in visStas:
                        self.staWidgets[visStas.index(sta)].removePicks(pickType,len(potIdxs)-pickCountMax)
        # Remove the picks from the hot variable pickSet
        keepIdxs=np.array([i for i in np.arange(len(self.hotVar['pickSet'].val)) if i not in delIdxs])
        self.hotVar['pickSet'].val=self.hotVar['pickSet'].val[keepIdxs]
        
    # Function to handle updates to the hot variable pickSet (assumes all picks have changed)
    # ... adding and remove picks on the current page (does not remove excess)
    def updatePagePicks(self):
        # Find out which of these values are unique (to remove duplicate picks)
        curList=[str(a[0])+str(a[1])+str(a[2]) for a in self.hotVar['pickSet'].val]
        unqCur,unqCurIdx=np.unique(curList,return_index=True)
        # Update the new set with only the unique picks
        self.hotVar['pickSet'].val=self.hotVar['pickSet'].val[unqCurIdx]
        # Remove any old plot lines
        for widget in self.staWidgets:
            for line in widget.pickLines:
                widget.pltItem.removeItem(line)
            widget.pickLines=[]
        # Add in the new lines
        stas=[widget.sta for widget in self.staWidgets]
        for sta,aType,aTime in self.hotVar['pickSet'].val:
            if sta not in stas:
                continue
            self.staWidgets[stas.index(sta)].addPick(aTime,aType,self.getPickPen(aType))
    
    # Return the wanted pick types pen, in RGB
    def getPickPen(self,aType):
        col=QtGui.QColor(self.pref['pickColor_'+aType].val)
        return (col.red(), col.green(), col.blue())
    
    # Match all streams channels to its appropriate pen
    def getStreamPens(self):
        penRef={} # Dictionary to return with all unique channels pens
        acceptKeys=[key for key in self.pref['customPen'].val.keys()]
        unqChas=np.unique([tr.stats.channel for tr in self.hotVar['stream'].val])
        # First check what tracePenAssign keys are actually accepted
        useKeys=[]
        for key in self.hotVar['tracePenAssign'].val.keys():
            # If the user returned default, let them know it is used if others do not exist
            if key=='default':
                print('customPen tag default is used as a catch all, ignored so does not overpower other trace pens')
            elif key in acceptKeys:
                useKeys.append(key)
            else:
                print('customPen tag '+key+ 'is not currently defined in preference customPen, applying default')
        # Loop through each unique channel and add its pen to penRef
        for cha in unqChas:
            colorInt,width=None,None
            matched=False
            # Check each of the entries for this key for a match
            for key,aList in [[aKey,self.hotVar['tracePenAssign'].val[aKey]] for aKey in useKeys]:
                for entry in aList:
                    if fnmatch(cha,entry):
                        colorInt,width,depth=self.pref['customPen'].val[key]
                        matched=True
                        break
                if matched:
                    break
            # If there was no match, apply default
            if colorInt==None:
                colorInt,width,depth=self.pref['customPen'].val['default']
            pen=mkPen(QtGui.QColor(colorInt),width=width)
            penRef[cha]=[pen,depth]
        return penRef
    
    # Gather the color assignment on a per station basis
    def getStaColors(self,assignType):
        # Only set the pen reference for stations which are present
        if assignType=='mapSta':
            if self.hotVar['staMeta'].val.shape[0]==0:
                unqStas=[]
            else:
                unqStas=np.unique(self.hotVar['staMeta'].val[:,0])
            defaultColInt=self.pref['defaultColorMapSta'].val
            assignKey='mapStaPenAssign'
        else:
            unqStas=np.unique([tr.stats.station for tr in self.hotVar['stream'].val])
            defaultColInt=self.pref['defaultColorTraceBg'].val
            assignKey='traceBgPenAssign'
        # Give empty staWidgets the default color
        colorRef={'':QtGui.QColor(defaultColInt)}
        # See which colors have been set
        useKeys=[]
        for key in self.hotVar[assignKey].val.keys():
            if key not in self.pref['customPen'].val.keys():
                print('customPen tag '+key+ 'is not currently defined in preference customPen, applying default')
            else:
                useKeys.append(key)
        # Assign stations colors (if none present, use defaultColInt)
        # Loop through each station and add its color to colorRef
        for sta in unqStas:
            colorInt,matched=None,False
            # Check each of the entries for this key for a match
            for key,aList in [[aKey,self.hotVar[assignKey].val[aKey]] for aKey in useKeys]:
                for entry in aList:
                    if fnmatch(sta,entry):
                        colorInt=self.pref['customPen'].val[key][0]
                        matched=True
                        break
                if matched:
                    break
            # If there was no match, apply default
            if colorInt==None:
                colorInt=defaultColInt
            colorRef[sta]=QtGui.QColor(colorInt)
        return colorRef
    
    # Update the trace curves on the current page
    def updateTraces(self):
        # As new channels could have been added, update the pen reference
        self.chaPenRef=self.getStreamPens()
        # Clear away all previous curves, and references to this station
        for i in np.arange(len(self.staWidgets)):
            for curve in self.staWidgets[i].traceCurves:
                self.staWidgets[i].removeItem(curve)
            self.staWidgets[i].traceCurves=[]
            self.staWidgets[i].pltItem.setLabel(axis='left',text='empty')
            self.staWidgets[i].sta=''
        # Add in the trace data for the current page
        i=0
        stas=np.array([tr.stats.station for tr in self.hotVar['pltSt'].val])
        numStas=len(np.unique(stas))
        while self.hotVar['curPage'].val*self.pref['staPerPage'].val+i<numStas:
            if i==self.pref['staPerPage'].val:
                break
            # Figure out which traces are associated with the next station in staSort
            thisSta=self.hotVar['staSort'].val[self.hotVar['curPage'].val*self.pref['staPerPage'].val+i]
            wantIdxs=np.where(stas==thisSta)[0]
            # Also set the y-limits
            ymin=np.min([np.min(self.hotVar['pltSt'].val[idx].data) for idx in wantIdxs])
            ymax=np.max([np.max(self.hotVar['pltSt'].val[idx].data) for idx in wantIdxs])
            self.staWidgets[i].setYRange(ymin,ymax)
            # Assign the widget a station code
            self.staWidgets[i].sta=thisSta
            self.staWidgets[i].pltItem.setLabel(axis='left',text=thisSta)
            # Plot the data
            for idx in wantIdxs:
                trace=self.hotVar['pltSt'].val[idx]
                pen,depth=self.chaPenRef[trace.stats.channel]
                self.staWidgets[i].addTrace(trace.times()+trace.stats.starttime.timestamp,trace.data,
                                            trace.stats.channel,pen,depth)
            i+=1
        # Set the background color
        for widget in self.staWidgets:                        
            widget.setBackground(self.traceBgColors[widget.sta])
    
    # Update the text label colors
    def updateTextColor(self,init=False):
        col=QtGui.QColor(self.pref['defaultColorText'].val)
        pen=mkPen(col)
        # Change all axis colors (axis line and ticks)
        for widget in self.staWidgets+[self.timeWidget,self.archiveEvent,
                                       self.archiveSpan]:
            widget.getPlotItem().getAxis('bottom').setPen(pen)
            widget.getPlotItem().getAxis('left').setPen(pen)
        self.mapWidget.setPen(pen)
        # Change pick file title color
        title=self.timeWidget.getPlotItem().titleLabel.text
        self.timeWidget.getPlotItem().titleLabel.setText(title,color=col)
    
    # Update all custom pens
    def updateCustomPen(self,init=False):
        self.updateTracePen()
        self.updateTraceBackground()
        self.updateMapStations()
    
    # Update just the trace pens on the current page
    def updateTracePen(self):
        self.chaPenRef=self.getStreamPens()
        for widget in self.staWidgets:
            for curve in widget.traceCurves:
                pen,depth=self.chaPenRef[curve.cha]
                curve.setPen(pen)
                curve.setZValue(depth)
                # If width of zero, do not show the curve
                if pen.widthF()==0:
                    curve.setVisible(False)
                else:
                    curve.setVisible(True)
    
    # Change the color of the trace background
    def updateTraceBackground(self,init=False):
        self.traceBgColors=self.getStaColors('traceBg')
        for aWidget in self.staWidgets:
            aWidget.setBackground(self.traceBgColors[aWidget.sta])
    
    # Change the color of time time axis
    def updateTimeBackground(self,init=False):
        col=QtGui.QColor(self.pref['defaultColorTimeBg'].val)
        self.timeWidget.setBackground(col)
    
    # Change the color of the archive axis
    def updateArchiveBackground(self,init=False):
        col=QtGui.QColor(self.pref['archiveColorBackground'].val)
        self.archiveSpan.setBackground(col)
        self.archiveEvent.setBackground(col)
        
    # Change the color of the archive availability boxes
    def updateArchiveAvailColor(self,init=False):
        col=QtGui.QColor(self.pref['archiveColorAvail'].val)
        for box in self.archiveSpan.boxes:
            box.setPen(col)
            
    # Change the color of the archive span select
    def updateArchiveSpanColor(self,init=False):
        col=QtGui.QColor(self.pref['archiveColorSpan'].val)
        rgba=(col.red(),col.green(),col.blue(),65)
        self.archiveSpan.span.setBrush(mkBrush(rgba))
        for line in self.archiveSpan.span.lines:
            line.setPen(col)
        
    # Change the color of the events in the archive visual...
    # ...selected, not selected, use same color as selected for archive span line hover
    def updateArchiveEveColor(self,init=False):
        col1=QtGui.QColor(self.pref['archiveColorSelect'].val)
        col2=QtGui.QColor(self.pref['archiveColorEve'].val)
        # Set current event line color
        if self.archiveEvent.curEveLine!=None:
            self.archiveEvent.curEveLine.setPen(mkPen(col1))
        # Set not-current event line color
        if self.archiveEvent.otherEveLine!=None:
            self.archiveEvent.otherEveLine.setPen(mkPen(col2))
        # Use the select for highlighting the span when hovered
        for line in self.archiveSpan.span.lines:
            line.setHoverPen(col1)
            
    # Update the color of the map axis
    def updateMapBackground(self,init=False):
        col=QtGui.QColor(self.pref['defaultColorMapBg'].val)
        self.mapWidget.setBackground(col)
    
    # Update the color of all of the station spots, with user specified colors
    def updateMapStations(self,init=False):
        # Assign the station colors for the stations in staMeta
        self.mapStaColors=self.getStaColors('mapSta')
        # Add the spots to the map
        self.mapWidget.loadStaMeta(self.hotVar['staMeta'].val,self.mapStaColors,init)
    
    # Update the current event spots on the map widget
    def updateMapCurEve(self):
        self.mapWidget.loadEvePoints(self.hotVar['mapCurEve'].val,'cur',
                                     QtGui.QColor(self.pref['defaultColorMapCurEve'].val))
    
    # Update the previous event spots on the map widget
    def updateMapPrevEve(self):
        self.mapWidget.loadEvePoints(self.hotVar['mapPrevEve'].val,'prev',
                                     QtGui.QColor(self.pref['defaultColorMapPrevEve'].val))
    
    # Update the color of the previous event symbols
    def updateMapPrevEveColor(self,init=False): 
        col=QtGui.QColor(self.pref['defaultColorMapPrevEve'].val)
        item=self.mapWidget.prevEveItem
        if item!=None:
            item.setBrush(mkBrush(col.red(),col.green(),col.blue(),160))
        
    # Update the color of the current event symbols
    def updateMapCurEveColor(self,init=False): 
        col=QtGui.QColor(self.pref['defaultColorMapCurEve'].val)
        item=self.mapWidget.curEveItem
        if item!=None:
            item.setBrush(mkBrush(col.red(),col.green(),col.blue(),200))
        
    # Update how many widgets are on the main page
    def updateStaPerPage(self,init=False):
        if not init:
            # Go back to the first page when updating number of staWidgets
            self.hotVar['curPage'].val=0
        # First remove the previous staWidgets
        for aWidget in self.staWidgets:
            aWidget.setParent(None)
            self.traceLayout.removeWidget(aWidget)
            self.staWidgets=[]
        axisPen=mkPen(QtGui.QColor(self.pref['defaultColorText'].val))
        # Add the desired number of staWidgets
        while len(self.staWidgets)<self.pref['staPerPage'].val:
            self.staWidgets.append(TraceWidget(self.mainLayout))
            self.staWidgets[-1].setXLink('timeAxis')
            self.traceLayout.addWidget(self.staWidgets[-1])
            # Connect the double click signal to the add pick signal
            self.staWidgets[-1].doubleClickSignal.connect(self.traceDoubleClickEvent)
            # Color the axes properly
            self.staWidgets[-1].getPlotItem().getAxis('left').setPen(axisPen)
        # If Lazylyst is initiating (first load), no need to update the page
        if not init:
            self.updatePage()
        
    # Set the archive availability
    def updateArchive(self):
        # Load in all of the start times of the archive...
        archiveFiles,archiveTimes=getArchiveAvail(self.hotVar['archDir'].val)
        # ...and sort them by start time (required for when extracting an events data)
        argSort=np.argsort(archiveTimes[:,0])
        archiveFiles,archiveTimes=archiveFiles[argSort],archiveTimes[argSort]
        self.hotVar['archFiles'].val=archiveFiles
        self.hotVar['archFileTimes'].val=archiveTimes
        # Update the time boxes
        self.archiveSpan.updateBoxes(archiveTimes,self.pref['archiveColorAvail'].val)
        # Do not bother changing the span if nowhere to go
        if len(archiveFiles)==0:
            print('No miniseed files in '+self.hotVar['archDir'].val)
            return
        # Set the initial span boundaries, and x-limits
        t1,t2=np.min(archiveTimes[:,0]),np.max(archiveTimes[:,1])
        buff=(t2-t1)*0.05
        self.archiveSpan.pltItem.setXRange(t1-buff,t2+buff)
        self.archiveSpan.span.setRegion((t1,t2))
        
    # Update the span text so user can set their bounds easier
    def updateSpanText(self):
        t0,t1=self.archiveSpan.getSpanBounds()
        self.archiveSpanT0Label.setText(str(t0)+'  to')
        self.archiveSpanT1Label.setText(' '+str(t1))
    
    # Update the pick list with events only in the selected span
    # ...this is done after both pickFiles and pickFileTimes are updated
    def updateArchiveSpanList(self):
        files=self.hotVar['pickFiles'].val
        times=self.hotVar['pickFileTimes'].val
        t1,t2=self.archiveSpan.span.getRegion()
        # See which files should be listed
        toListFiles=files[np.where((times>=t1)&(times<=t2))]
        # Reload the pick file list
        self.archiveList.clear()
        self.archiveList.addItems(toListFiles)
        
    # Update the map with the new station metadata
    def updateStaMeta(self):
        # Load in the new station metadata
        self.hotVar['staMeta'].val=np.genfromtxt(self.hotVar['staFile'].val,delimiter=',',dtype=str)
        # If the file was empty or just one line, try and convert to appropriate shape
        if 0 in self.hotVar['staMeta'].val.shape:
            self.hotVar['staMeta'].val=np.empty((0,4))
        elif len(self.hotVar['staMeta'].val.shape)==1:
            self.hotVar['staMeta'].val=np.array([self.hotVar['staMeta'].val])
        self.updateMapStations(init=True)
        # Reset any additional map related visuals
        defaultHot=initHotVar()
        for key in ['mapCurEve','mapPrevEve']:
            self.hotVar[key].val=defaultHot[key].val
            self.hotVar[key].update()
        
    # Update the selected (double clicked) station on the map view
    def updateMapSelectSta(self):
        self.hotVar['curMapSta'].val=self.mapWidget.selectSta
        
    # Load the pick file list for display, given completly new pick directory
    def updatePickDir(self):
        # Clear the old list
        self.archiveList.clear()
        self.hotVar['pickFiles'].val=np.empty((0),dtype=str)
        # Reset the current pick file
        self.hotVar['curPickFile'].val=''
        self.hotVar['curPickFile'].update()
        # Only accept files with proper naming convention
        for aFile in sorted(os.listdir(self.hotVar['pickDir'].val)):
            aFile=str(aFile)
            splitFile=aFile.split('_')
            # Make sure has the proper extension '.picks'
            if len(splitFile)!=2 or aFile.split('.')[-1]!='picks':
                continue
            try:
                int(splitFile[0])
                getTimeFromFileName(aFile).timestamp
            except:
                continue
            self.hotVar['pickFiles'].val=np.concatenate((self.hotVar['pickFiles'].val,np.array([aFile])))
        # Sort the pick files
        self.updateEveSort()
        # Update the archive span widget
        self.archiveEvent.updateEveLines(self.hotVar['pickFileTimes'].val,self.hotVar['curPickFile'].val,
                                         self.pref['archiveColorEve'].val,self.pref['archiveColorSelect'].val)
    
    # With the same pick directory, force an update on the pick files...
    # ...this allows for addition of empty pick files and deletion of pick files
    def updatePickFiles(self):
        # Ensure that pick files returned are in wanted order
        self.updateEveSort()
        # Reset pick file time lines in archiveEvent
        self.archiveEvent.updateEveLines(self.hotVar['pickFileTimes'].val,self.hotVar['curPickFile'].val,
                                         self.pref['archiveColorEve'].val,self.pref['archiveColorSelect'].val)
        # See which files were present prior to the update
        prevFiles=sorted(os.listdir(self.hotVar['pickDir'].val))
        # Delete events which are no longer present
        for aFile in prevFiles:
            path=self.hotVar['pickDir'].val+'/'+aFile
            if (aFile not in self.hotVar['pickFiles'].val) and os.path.exists(path):
                os.remove(path)
        # Add blank pick files which were not present before
        for aFile in self.hotVar['pickFiles'].val:
            if aFile not in prevFiles:
                newFile=open(self.hotVar['pickDir'].val+'/'+aFile,'w')
                newFile.close()
        # Update the current pick file if it is no longer in the pick files
        if self.hotVar['curPickFile'].val not in self.hotVar['pickFiles'].val:
            # Do not bother updating curPickFile if not set
            if self.hotVar['curPickFile'].val!='':
                self.hotVar['curPickFile'].val=''
                self.hotVar['curPickFile'].update()
            
    # Sort the pick files according to user preference...
    # ...also update the pickFileTimes
    def updateEveSort(self,init=False):
        # Sort alphabetically first (will be the secondary sorting)
        self.hotVar['pickFiles'].val=np.sort(self.hotVar['pickFiles'].val)
        self.hotVar['pickFileTimes'].val=np.array([getTimeFromFileName(aFile).timestamp for aFile in self.hotVar['pickFiles'].val])
        # If sorting by ID, this is the same as alphabetical (unless the ID is huge)
        if self.pref['eveSortStyle'].val=='id':
            pass
        elif self.pref['eveSortStyle'].val=='time':
            argSort=np.argsort(self.hotVar['pickFileTimes'].val)
            self.hotVar['pickFiles'].val=self.hotVar['pickFiles'].val[argSort]
            self.hotVar['pickFileTimes'].val=self.hotVar['pickFileTimes'].val[argSort]
        else:
            print('The eveSortStyle '+self.pref['eveSortStyle'].val+' has not been implemented, sorting by id')
        # Clear the old GUI list, and add the new items
        self.updateArchiveSpanList()
    
    # With the same source information, go to the current pick file
    def updateCurPickFile(self):
        if self.hotVar['curPickFile'].val!='':
            # If the file does not exist, create it
            if self.hotVar['curPickFile'].val not in self.hotVar['pickFiles'].val:
                newFile=open(self.hotVar['pickDir'].val+'/'+self.hotVar['curPickFile'].val,'w')
                newFile.close()
                # Add this event to pickFiles
                self.hotVar['pickFiles'].val=np.concatenate((self.hotVar['pickFiles'].val,
                                                             np.array([self.hotVar['curPickFile'].val])))
                self.hotVar['pickFiles'].update()
        self.updateEvent()
    
    # As the number of pick types can change in the settings...
    # ...show less/more pick type color preferences
    def updatePickColorPrefs(self,init=False):
        curPickTypes=[key for key in self.pref['pickTypesMaxCountPerSta'].val]
        curPickColors=[key for key in self.pref.keys() if
                       ('pickColor_' in str(key) and self.pref[key].dialog=='ColorDialog')]
        # Add the pick color preferences
        for aType in curPickTypes:
            # Do not overwrite if already present
            tag='pickColor_'+aType
            if tag in curPickColors:
                continue
            # Otherwise add the color
            self.pref[tag]=Pref(tag=tag,val=4294967295,dataType=int,
                            dialog='ColorDialog',func=self.updatePage)
            # If Lazylyst is starting, still have to load in the previous pick colors (if set already)
            if init:
                prefVals=self.setPref.value('prefVals',{})
                if tag in [key for key in prefVals.keys()]:
                    self.pref[tag].val=prefVals[tag]
                # Give some default colors, to the default pick types
                elif tag =='pickColor_P':
                    self.pref[tag].val=65280
                elif tag =='pickColor_S':
                    self.pref[tag].val=16776960
        # Remove any entries which may have been taken away from curPickTypes
        for tag in curPickColors:
            if tag.split('_')[1] not in curPickTypes:
                self.pref.pop(tag)
        # Update the configuration dialogs preference list
        if not init:
            self.dialog.confPrefList.clear()
            self.dialog.confPrefList.addItems([key for key in self.pref.keys()])
        # If the user deleted a pick type with them already present on screen, remove them
        for aType in np.unique(self.hotVar['pickSet'].val[:,1]):
            if aType not in curPickTypes:
                self.hotVar['pickSet'].val=self.remUnknownPickTypes(self.hotVar['pickSet'].val)
                self.hotVar['pickSet'].update()
                break
    
    # Update the strolling list with all in-use timed actions
    def updateStrollingList(self):
        strollers=self.qTimers.keys()
        # Update the label (header)
        self.strollingLabel.setText('Strolling Actions ('+str(len(strollers))+')')
        # Reset the list
        self.strollComboBox.clear()
        self.strollComboBox.addItems(strollers)
    
    # Set a span bound to user specified value
    def setSpanBoundViaDialog(self,whichBound):
        curBounds=self.archiveSpan.span.getRegion()
        if whichBound=='T0':
            bound=curBounds[0]
        else:
            bound=curBounds[1]
        newBound,ok=DateDialog.getDateTime(bound)
        # If the dialog was not cancelled...
        if not ok:
            return
        # Set the new bound
        elif whichBound=='T0':
            self.archiveSpan.span.setRegion((newBound,curBounds[1]))
        else:
            self.archiveSpan.span.setRegion((curBounds[0],newBound))
    
    # Load setting from previous run, and initialize base variables
    def loadSettings(self):
        # Load the hot variables
        self.hotVar=initHotVar()
        # Get this scripts path, as functions will be relative to this
        mainPath=os.path.dirname(os.path.realpath(__file__))
        self.hotVar['mainPath'].val=mainPath
        for key,hotVar in iteritems(self.hotVar):
            hotVar.linkToFunction(self)
        # Get all values from settings
        self.setGen=QSettings(mainPath+'/setGen.ini', QSettings.IniFormat)
        self.setAct=QSettings(mainPath+'/setAct.ini', QSettings.IniFormat)
        self.setPref=QSettings(mainPath+'/setPref.ini', QSettings.IniFormat)
        self.setSource=QSettings(mainPath+'/setSource.ini', QSettings.IniFormat)
        # UI size
        self.resize(self.setGen.value('size', QtCore.QSize(1300, 700)))
        # Actions...
        self.act=self.setAct.value('actions', defaultActions())
        self.actPassiveOrder=self.setAct.value('actPassiveOrder', defaultPassiveOrder(self.act))
        if self.actPassiveOrder==None:
            self.actPassiveOrder=[]
        # ...link all actions to their appropriate functions
        for key,action in iteritems(self.act):
            action.linkToFunction(self)
        # Preferences (start)
        self.pref=defaultPreferences(self)
        prefVals=self.setPref.value('prefVals',{})
        # Preferences (finish)
        for aKey in prefVals.keys():
            # Skip any preferences which are generated a bit later
            if 'pickColor_' in aKey:
                continue
            self.pref[aKey].val=prefVals[aKey]
        # Saved sources
        self.saveSource=self.setSource.value('savedSources',{})
        # Create empty variables
        self.staWidgets=[]
        self.qTimers={}
        
    # Save all settings from current run
    def saveSettings(self):
        # UI size
        self.setGen.setValue('size', self.size())
        # Actions, cannot save functions (will be linked again upon loading)
        for key in self.act.keys():
            self.act[key].func=None
        self.setAct.setValue('actions',self.act)
        self.setAct.setValue('actPassiveOrder',self.actPassiveOrder)
        # Preferences
        prefVals={}
        for aKey in self.pref.keys():
            prefVals[str(self.pref[aKey].tag)]=self.pref[aKey].val
        self.setPref.setValue('prefVals',prefVals)
        # Saved sources
        self.setSource.setValue('savedSources',self.saveSource)
        
    # For actions which are triggered via built-in qt events
    def passAction(self):
        pass
    
    # When the GUI closes, will save to settings
    def closeEvent(self,ev):
        # Run any actions which are to be done before closing
        self.processAction(self.act['CloseLazylyst'])
        self.saveSettings()
        ev.accept()
        
# Class for logging
class QtHandler(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
    def emit(self, record):
        record = self.format(record)
        if record: XStream.stdout().write('%s\n'%record)
    
class XStream(QtCore.QObject):
    _stdout = None
    _stderr = None
    messageWritten = QtCore.pyqtSignal(str)
    def flush( self ):
        pass
    def fileno( self ):
        return -1
    def write( self, msg ):
        if ( not self.signalsBlocked() ):
            self.messageWritten.emit(str(msg))
    @staticmethod
    def stdout():
        if ( not XStream._stdout ):
            XStream._stdout = XStream()
            sys.stdout = XStream._stdout
        return XStream._stdout
    @staticmethod
    def stderr():
        if ( not XStream._stderr ):
            XStream._stderr = XStream()
            sys.stderr = XStream._stderr
        return XStream._stderr

# Start up the logging and UI
if __name__ == '__main__':
    # For sending stdout to the trace log
    logger = logging.getLogger(__name__)
    handler = QtHandler()
    handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    # Start up the UI
    app = QtGui.QApplication(sys.argv)
    window = LazylystMain()
    window.show()
    sys.exit(app.exec_())
