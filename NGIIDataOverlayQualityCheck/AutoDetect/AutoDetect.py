# -*- coding: utf-8 -*-
import os
import ConfigParser
import psycopg2
from psycopg2.sql import SQL, Identifier
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import threading
import platform
import subprocess
import time
import io

from PyQt4.QtCore import *
from PyQt4.QtGui import *
from PyQt4 import QtGui, uic

from qgis.core import *
from osgeo import gdal, ogr, osr

from .. calc_utils import force_gui_update, findEncoding

###############################
### OS 커멘드를 백그라운드로 실행하는 클래스
class CmdThread(threading.Thread):
    def __init__(self, commandList):
        self.commandList = commandList
        threading.Thread.__init__(self)

    def run(self):
        if platform.system() == 'Windows':
            subprocess.check_call(self.commandList, creationflags=0x08000000)
        else:
            subprocess.check_call(self.commandList, stderr=subprocess.STDOUT)
        return

def threadExecuteCmd(commandList):
    trd = CmdThread(commandList)
    trd.start()

    while threading.activeCount() > 1:
        QgsApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        time.sleep(0.1)


#######################
### 시간이 오래 걸리는 쿼리를 쓰레드로 돌리는 크래스
class DBThread(threading.Thread):
    def __init__(self, cursor, query, param):
        self.cursor = cursor
        self.query = query
        self.param = param
        threading.Thread.__init__(self)

    def run(self):
        if self.param is None:
            self.cursor.execute(self.query)
        else:
            self.cursor.execute(self.query, self.param)

        return

def threadExecuteSql(cursor, sql, param=None):
    dbt = DBThread(cursor, sql, param)
    dbt.start()

    while threading.activeCount() > 1:
        QgsApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        time.sleep(0.1)


#######################
### DB 접속정보를 입력받는 UI
DbInfoDialog_FORM_CLASS, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), 'DbInfo.ui'))

class DbInfoDialog(QDialog, DbInfoDialog_FORM_CLASS):

    inspectWidget = None
    __oldDataDir = None

    def __init__(self, parent=None):
        super(DbInfoDialog, self).__init__(parent)
        self.setupUi(self)
        self.parent = parent


#######################
### 자동탐지 결과를 저장할 경로 받는 UI
ResSaveDialog_FORM_CLASS, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), 'ResSave.ui'))

class ResSaveDialog(QDialog, ResSaveDialog_FORM_CLASS):
    _mode = None

    def __init__(self, parent, dlgAutoDetect):
        super(ResSaveDialog, self).__init__(parent)
        self.setupUi(self)
        self.parent = parent
        self.dlgAutoDetect = dlgAutoDetect

        self.setMode("shp")

        self.__connectFn()

    # 이벤트 처리로 파일종류 선택에 대응
    def __connectFn(self):
        self.rdoShp.toggled.connect(self.on_rdoShp)
        self.rdoGpkg.toggled.connect(self.on_rdoGpkg)
        self.btnBrowse.clicked.connect(self.on_click_btnBrowse)
        self.btnOk.clicked.connect(self.on_accepted)
        self.btnCancel.clicked.connect(self.on_rejected)

    def setMode(self, mode):
        if mode == "shp":
            self.label.setText(u"변화정보 저장 Shape 파일 폴더: ")
            self.edtPath.setText("")
            self._mode = "shp"
        else:
            self.label.setText(u"변화정보 저장 GeoPackage 파일: ")
            self.edtPath.setText("")
            self._mode = "gpkg"

    def on_accepted(self):
        path = self.edtPath.text()
        if path == "":
            msg = QMessageBox()
            msg.setWindowTitle(u"공간정보 중첩 검사 툴")
            msg.setIcon(QMessageBox.Information)
            msg.setText(u"내보내기 형태와 파일 혹은 폴더를 선택하셔야 진행 가능합니다.")
            msg.setStandardButtons(QMessageBox.Ok)

            msg.exec_()
        else:
            self.accept()

    def on_rejected(self):
        msg = QMessageBox()
        msg.setWindowTitle(u"공간정보 중첩 검사 툴")
        msg.setIcon(QMessageBox.Question)
        msg.setText(u"만약 지금 취소하시면 다시 [기초자료/수정성과 비교]를 수행하셔야 변화정보 저장파일을 만들 수 있습니다.\n"
                    u"정말 취소하시겠습니까?")
        msg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)

        rc = msg.exec_()
        if rc == QMessageBox.Ok:
            self.reject()

    def on_rdoShp(self, checked):
        if checked:
            self.setMode("shp")

    def on_rdoGpkg(self, checked):
        if checked:
            self.setMode("gpkg")

    def on_click_btnBrowse(self):
        if self._mode == "shp":
            dialog = QtGui.QFileDialog(self)
            shpDir = dialog.getExistingDirectory(self, u"변화정보를 저장할 ESRI Shape 폴더 선택")

            if not shpDir:
                return
            self.edtPath.setText(shpDir)

        elif self._mode == "gpkg":
            dialog = QtGui.QFileDialog(self)
            gpkgFile = dialog.getSaveFileName(self, u"변화정보를 저장할 GeoPackage(GPKG) 파일 선택", None, "GeoPackage(*.gpkg)")

            if not gpkgFile:
                return

            self.edtPath.setText(gpkgFile)

    def run_export(self):
        pgConnectInfo = 'PG:host={host} port={port} dbname={dbname} user={user} password={password}' \
            .format(host=self.dlgAutoDetect.dbHost, port=self.dlgAutoDetect.dbPort, dbname=self.dlgAutoDetect.dbNm,
                    user=self.dlgAutoDetect.dbUser, password=self.dlgAutoDetect.dbPassword)

        # 좌표계 정보 생성
        crs = osr.SpatialReference()
        crs.ImportFromEPSG(5179)

        # DB 접속
        try:
            pg = gdal.OpenEx(pgConnectInfo, gdal.OF_VECTOR, ["PostgreSQL"],
                             ['PRECISION=NO'])
        except:
            self.parent.error(u"DB 접속데 실패하였습니다.")
            return

        shpDir = self.edtPath.text()

        layerList = [self.dlgAutoDetect.lstOriginData.item(i).text() for i in range(self.dlgAutoDetect.lstOriginData.count())]

        try:
            QgsApplication.setOverrideCursor(Qt.WaitCursor)
            self.parent.info(u"변화정보 납품파일 생성 시작")
            self.parent.lblStatus.setText(u"변화정보 납품파일 생성 중...")
            self.parent.prgMain.setMaximum(0)

            self.parent.prgSub.setMaximum(len(layerList))
            i = 0
            for layerName in layerList:
                i += 1
                self.parent.prgSub.setValue(i)
                force_gui_update()

                shpFilePath = os.path.join(shpDir, layerName) + ".shp"

                sql = u"""
                select *
                from (
                (
                    select edit.*, auto.mod_type
                    from qi_edit.{layer} as edit, qi_edit.inspect_obj as auto
                    where
                      edit.ogc_fid = auto.receive_ogc_fid
                      and auto.layer_nm = '{layer}'
                    )
                    union
                    (
                    select org.*, auto.mod_type
                    from qi_origin.{layer} as org, qi_edit.inspect_obj as auto
                    where
                      org.ogc_fid = auto.origin_ogc_fid
                      and auto.layer_nm = '{layer}'
                      and auto.mod_type = 'r'
                    )
                ) as uni
                            """.format(layer=layerName)
                pgLayer = pg.ExecuteSQL(sql.encode("UTF8"))

                try:
                    outDriver = ogr.GetDriverByName("ESRI Shapefile")
                    outDataSource = outDriver.CreateDataSource(shpFilePath,
                                                               ["SHAPE_ENCODING=UTF8", "ENCODING=UTF8", 'PRECISION=NO'])

                    # 객체 타입 설정
                    geomType = pgLayer.GetGeomType()
                    shpLayer = outDataSource.CreateLayer(layerName.encode("UTF8"), geom_type=geomType)

                    pgLayerDefn = pgLayer.GetLayerDefn()
                    for i in range(pgLayerDefn.GetFieldCount()):
                        fieldDefn = pgLayerDefn.GetFieldDefn(i)
                        shpLayer.CreateField(fieldDefn)

                    outLayerDefn = shpLayer.GetLayerDefn()

                    for inFeature in pgLayer:
                        # Create output Feature
                        outFeature = ogr.Feature(outLayerDefn)

                        # Add field values from input Layer
                        for i in range(0, outLayerDefn.GetFieldCount()):
                            fieldDefn = outLayerDefn.GetFieldDefn(i)
                            fieldName = fieldDefn.GetName()

                            outFeature.SetField(outLayerDefn.GetFieldDefn(i).GetNameRef(),
                                                inFeature.GetField(i))

                        # Set geometry as centroid
                        geom = inFeature.GetGeometryRef()
                        outFeature.SetGeometry(geom.Clone())
                        # Add new feature to output Layer
                        shpLayer.CreateFeature(outFeature)
                        outFeature = None

                    shpLayer = None

                    crs.MorphToESRI()
                    file = open(os.path.join(shpDir, layerName) + '.prj', 'w')
                    file.write(crs.ExportToWkt())
                    file.close()

                    # 이 코드만 넣으면 이상하게 한글이 깨진다.
                    # file = open(os.path.join(shpDir, layerName) + '.cpg', 'w')
                    # file.write("UTF-8")
                    # file.close()
                except Exception as e:
                    self.parent.error(u"내보내기 하는 파일 생성 중 오류가 발생했습니다.")
                    continue
                    # raise e
        except Exception as e:
            raise e
        finally:
            QgsApplication.restoreOverrideCursor()

            self.parent.info(u"변화정보 납품파일 생성 완료")
            self.parent.lblStatus.setText(u"")
            self.parent.prgSub.setValue(0)
            self.parent.prgMain.setMaximum(100)
            self.parent.alert(u"변화정보 내보내기 완료")


#############################
### 변화 자동탐지 UI
AutoDetect_FORM_CLASS, _ = uic.loadUiType(os.path.join(os.path.dirname(__file__), 'AutoDetect.ui'))

class AutoDetect(QDialog, AutoDetect_FORM_CLASS):

    PROPERTIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pgInfo", "connection.ini")
    SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pgInfo", "sql.ini")

    ORIGIN_SCHEMA = 'qi_origin'
    EDIT_SCHEMA = 'qi_edit'

    conn = None
    sqlStatement = None

    _last_opened_org_folder = None
    _last_opened_edit_folder = None

    def show(self):
        try:
            self.lblOrgFolder.setText("Folder: ")
            self.lblEditFolder.setText("Folder: ")
            self.lstOriginData.clear()
            self.lstEditData.clear()
        except:
            self.parent.error(u"Auto Detect 초기화 오류")

        super(AutoDetect, self).show()

    def __init__(self, iface, parent=None):
        super(AutoDetect, self).__init__(parent)
        self.setupUi(self)

        self.iface = iface
        self.parent = parent

        self.progressMain = parent.prgMain
        self.progressSub = parent.prgSub
        self.lblStatus = parent.lblStatus

        self.__connectFn()

    def __connectFn(self):
        self.btnFindOriginData.clicked.connect(self.findOriginData)
        self.btnFindEditData.clicked.connect(self.findEditData)
        self.btnFindDiff.clicked.connect(self.findDiff)

    def connectPg(self):
        self.__loadProperties()
        self.__loadSql()

        try:
            if self.conn:
                self.conn.close()

            self.conn = psycopg2.connect(host=self.dbHost, port=int(self.dbPort), database=self.dbNm,
                                         user=self.dbUser, password=self.dbPassword)

        except Exception as e:
            return False

        return True

    def __loadProperties(self):
        self.properties = ConfigParser.RawConfigParser()
        self.properties.read(self.PROPERTIES_FILE)

        # Database
        self.dbHost = self.properties.get("database", "host")
        self.dbPort = self.properties.get("database", "port")
        self.dbNm = self.properties.get("database", "dbname")
        self.dbUser = self.properties.get("database", "user")
        self.dbPassword = self.properties.get("database", "password")

    def __loadSql(self):
        self.sqlStatement = ConfigParser.RawConfigParser()
        self.sqlStatement.read(self.SQL_FILE)

    def checkEnv(self):
        if platform.system() == 'Windows':
            from _winreg import *

            conReg = ConnectRegistry(None, HKEY_LOCAL_MACHINE)
            # check PostgreSQL
            try:
                postgresqlKey = "SOFTWARE\PostgreSQL"
                OpenKey(conReg, postgresqlKey)

            except:
                QMessageBox.warning(self.iface.mainWindow(), u"경고",
                                    u"PostgreSQL이 설치되어 있지 않습니다.<br>"
                                    u"아래 링크에서 9.6.x 버전 설치 파일을 받아 설치해주시기 바랍니다.<br><br>"
                                    u"<a href='https://www.enterprisedb.com/downloads/postgres-postgresql-downloads'>"
                                    u"PostgreSQL 다운로드</a>")
                return False

            # check PostGIS
            try:
                postgisKey = "SOFTWARE\PostGIS"
                OpenKey(conReg, postgisKey)

            except:
                QMessageBox.warning(self.iface.mainWindow(), u"경고",
                                    u"PostGIS가 설치되어 있지 않습니다.<br>"
                                    u"아래 링크에서 2.4.x 버전 설치 파일을 받아 설치해주시기 바랍니다.<br><br>"
                                    u"<a href='https://winnie.postgis.net/download/windows/pg96/buildbot/'>"
                                    u"PostGIS 다운로드</a>")
                return False

        if not os.path.exists(self.PROPERTIES_FILE):
            dlg = DbInfoDialog(self.iface.mainWindow())
            rc = dlg.exec_()
            if rc != QDialog.Accepted:
                QMessageBox.warning(self.iface.mainWindow(), u"경고", u"DB 접속정보가 입력되지 않아 프로그램을 계속할 수 없습니다.")
                os.remove(self.PROPERTIES_FILE)
                return False
            else:
                self.createIniFile(dlg)

        if not self.initDatabase():
            return False

        return True

    def createIniFile(self, dlg):
        iniExample = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pgInfo", "connection.ini.example")
        config = ConfigParser.RawConfigParser()
        try:
            config.read(iniExample)
            config.set("database", "host", dlg.edtHost.text())
            config.set("database", "port", dlg.edtPort.text())
            config.set("database", "dbname", dlg.edtDatabase.text())
            config.set("database", "user", dlg.edtUser.text())
            config.set("database", "password", dlg.edtPassword.text())
            with open(self.PROPERTIES_FILE, 'w') as fout:
                config.write(fout)
        except Exception as e:
            print(e)

    def initDatabase(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            properties = ConfigParser.RawConfigParser()
            properties.read(self.PROPERTIES_FILE)

            # Database
            host = properties.get("database", "host")
            port = properties.get("database", "port")
            dbname = properties.get("database", "dbname")
            user = properties.get("database", "user")
            password = properties.get("database", "password")

            rc = self.initDatbase(host, port, dbname, user, password)
            if rc:
                QApplication.restoreOverrideCursor()
                QMessageBox.about(self.iface.mainWindow(), u"오류", u"DB에 접속할 수 없어 중단됩니다.\n{}".format(rc))
                os.remove(self.PROPERTIES_FILE)
                return False
        except Exception as e:
            print(e)
        finally:
            QApplication.restoreOverrideCursor()

        return True

    def initDatbase(self, host, port, dbname, user, password):
        postgres_postgres_conn_str = u"host='{}' port='{}' dbname ='postgres' user='{}' password='{}'".format(host,
                                                                                                              port,
                                                                                                              user,
                                                                                                              password)
        sql_espg_file = os.path.join(os.path.dirname(__file__), "..", "pgInfo", "postgis_korea_epsg_towgs84.sql")

        # Read SQL files
        try:
            with io.open(sql_espg_file, 'r', encoding='utf8') as f:
                sql_espg = f.read()
        except:
            return "SQL File read error"

        try:
            # Make the ngii login role and database
            try:
                conn = psycopg2.connect(postgres_postgres_conn_str)
                conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            except:
                return u"error database connection: {}".format(postgres_postgres_conn_str)

            curs = conn.cursor()
            curs.execute(u"SELECT datname FROM pg_database WHERE datname = '{}'".format(dbname))
            results = curs.fetchall()
            if len(results) != 1:
                sql = u"CREATE DATABASE {} WITH ENCODING='UTF8' TEMPLATE=template0 LC_COLLATE='C' LC_CTYPE='C' CONNECTION LIMIT=-1".format(
                    dbname)
                curs.execute(sql)
            conn.close()

            tmp_postgres_conn_str = u"host='{}' port='{}' dbname ='{}' user='{}' password='{}'".format(host,
                                                                                                              port,
                                                                                                              dbname,
                                                                                                              user,
                                                                                                              password)

            # Setup extension and EPSG
            try:
                print tmp_postgres_conn_str
                conn = psycopg2.connect(tmp_postgres_conn_str)
                conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            except:
                return u"error database connection: {}".format(tmp_postgres_conn_str)

            curs = conn.cursor()
            curs.execute("SELECT extname FROM pg_extension WHERE extname = 'postgis'")
            results = curs.fetchall()
            if len(results) != 1:
                sql = u"CREATE EXTENSION postgis"
                threadExecuteSql(curs, sql)
                curs.execute(sql_espg)

            conn.close()
        except Exception as e:
            return e

        return None

    def getGpkgLayerList(self, gpkgPath, listUi):
        gpkg = None

        try:
            # TODO: 속도 향상에 아래 문장이 매우 중요. 다른 곳에도 적용하자!!
            gdal.SetConfigOption('OGR_SQLITE_SYNCHRONOUS', 'OFF')
            gpkg = ogr.Open(gpkgPath)
            if not gpkg:
                raise Exception()

            # Load Layer
            for layer in gpkg:
                layerName = unicode(layer.GetName().decode('utf-8'))
                item = QListWidgetItem(layerName)
                listUi.addItem(item)

        except Exception as e:
            raise e
        finally:
            QgsApplication.restoreOverrideCursor()
            self.progressMain.setValue(0)
            self.progressSub.setValue(0)
            del gpkg

    def findOriginData(self):
        dialog = QtGui.QFileDialog(self)
        dialog.setFileMode(QtGui.QFileDialog.ExistingFiles)

        if self._last_opened_org_folder is not None:
            dialog.setDirectory(self._last_opened_org_folder)

        filters = [u"국토지리정보 파일(*.gpkg *.shp)"]
        dialog.setNameFilters(filters)

        if (dialog.exec_()):
            fileList = dialog.selectedFiles()
        else:
            return

        shpList = list()
        gpkgList = list()

        for vectorPath in fileList:
            filename, extension = os.path.splitext(vectorPath)

            if extension.lower() == ".shp":
                shpList.append(vectorPath)
            elif extension.lower() == ".gpkg":
                gpkgList.append(vectorPath)

        if len(shpList) != 0 and len(gpkgList) != 0:
            self.parent.alert(u"GeoPackage 파일과 Shape 파일을 동시에 선택하실 수 없습니다.")
            return

        if len(shpList) == 0 and len(gpkgList) > 1:
            self.parent.alert(u"GeoPackage 파일은 여러개를 동시선택 하실 수 없습니다.")
            return

        self.lstOriginData.clear()
        if len(shpList):
            for shpPath in shpList:
                folder, fileName = os.path.split(shpPath)
                basename, ext = os.path.splitext(fileName)

                self._last_opened_org_folder = folder
                item = QListWidgetItem(basename)
                self.lstOriginData.addItem(item)

            self.lblOrgFolder.setText(self._last_opened_org_folder)

            return

        gpkgPath = gpkgList[0]
        self.getGpkgLayerList(gpkgPath, self.lstOriginData)
        self._last_opened_org_folder = gpkgPath
        self.lblOrgFolder.setText(self._last_opened_org_folder)


    def findEditData(self):
        dialog = QtGui.QFileDialog(self)
        dialog.setFileMode(QtGui.QFileDialog.ExistingFiles)

        if self._last_opened_edit_folder is not None:
            dialog.setDirectory(self._last_opened_edit_folder)

        filters = [u"국토지리정보 파일(*.gpkg *.shp)"]
        dialog.setNameFilters(filters)

        if (dialog.exec_()):
            fileList = dialog.selectedFiles()
        else:
            return

        shpList = list()
        gpkgList = list()

        for vectorPath in fileList:
            filename, extension = os.path.splitext(vectorPath)

            if extension.lower() == ".shp":
                shpList.append(vectorPath)
            elif extension.lower() == ".gpkg":
                gpkgList.append(vectorPath)

        if len(shpList) != 0 and len(gpkgList) != 0:
            self.parent.alert(u"GeoPackage 파일과 Shape 파일을 동시에 선택하실 수 없습니다.")
            return

        if len(shpList) == 0 and len(gpkgList) > 1:
            self.parent.alert(u"GeoPackage 파일은 여러개를 동시선택 하실 수 없습니다.")
            return

        self.lstEditData.clear()
        if len(shpList):
            for shpPath in shpList:
                folder, fileName = os.path.split(shpPath)
                basename, ext = os.path.splitext(fileName)

                self._last_opened_edit_folder = folder
                item = QListWidgetItem(basename)
                self.lstEditData.addItem(item)

            self.lblEditFolder.setText(self._last_opened_edit_folder)
            return

        gpkgPath = gpkgList[0]
        self.getGpkgLayerList(gpkgPath, self.lstEditData)
        self._last_opened_edit_folder = gpkgPath
        self.lblEditFolder.setText(self._last_opened_edit_folder)

    def findDiff(self):
        if self.lstOriginData.count() == 0:
            self.parent.alert(u"기초자료 레이어를 하나이상 선택하셔야만 시작 가능합니다.")
            return

        if self.lstEditData.count() == 0:
            self.parent.alert(u"수정성과 레이어를 하나이상 선택하셔야만 시작 가능합니다.")
            return


        flagNotSame = False

        if self.lstEditData.count() != self.lstOriginData.count():
            flagNotSame = True
        else:
            for i in range(self.lstOriginData.count()):
                iOrgLayer = self.lstOriginData.item(i).text()
                iEditLayer = self.lstEditData.item(i).text()
                
                if iOrgLayer != iEditLayer:
                    flagNotSame = True
                    break
                    
        if flagNotSame:
            self.parent.alert(u"기초자료와 수정성과의 레이어 쌍이 맞지 않아 중단됩니다.")
            return

            # TODO: 아래처럼 되게 하기
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Question)
            msg.setWindowTitle(u"국토기본정보 공간정보 중첩 검사 툴")
            msg.setText(u"기초자료와 수정성과의 레이어 쌍이 맞지 않습니다.\n"
                        u"기초자료에만 있는 레이어의 객체는 모두 삭제된 것으로, \n"
                        u"수정성과에만 있는 레이어의 객체는 모두 생성된 것으로 처리됩니다.\n"
                        u"\n"
                        u"그래도 계속 진행하시겠습니까?")
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)

            rc = msg.exec_()
            if rc != QMessageBox.Yes:
                return

        if self._last_opened_org_folder == self._last_opened_edit_folder:
            self.parent.alert(u"기초자료와 수정성과가 같은 폴더의 같은 파일들이면 안됩니다.")
            return

        # 자동비교 대화상자 닫기
        self.close()

        self.progressMain.setMinimum(0)
        self.progressMain.setMaximum(0)

        self.lblStatus.setText(u"자료 확인 중 ... ")
        QgsApplication.setOverrideCursor(Qt.WaitCursor)

        try:
            if self.ORIGIN_SCHEMA in self.selectSchemaList():
                self.dropSchema(self.ORIGIN_SCHEMA)

            if self.EDIT_SCHEMA in self.selectSchemaList():
                self.dropSchema(self.EDIT_SCHEMA)

            # 기성과 insert
            if self.lblOrgFolder.text()[-5:].lower() == ".gpkg":
                dataType = "gpkg"
            else:
                dataType = "shp"

            res = self.insertData(self.ORIGIN_SCHEMA, dataType, self.lblOrgFolder.text(), [self.lstOriginData.item(i).text() for i in range(self.lstOriginData.count())])
            if not res:
                self.parent.error(u"오류가 발생하여 기초자료/수정성과 비교가 중단되었습니다.")
                return

            # 남품성과 insert
            if self.lblEditFolder.text()[-5:].lower() == ".gpkg":
                dataType = "gpkg"
            else:
                dataType = "shp"

            res = self.insertData(self.EDIT_SCHEMA, dataType, self.lblEditFolder.text(), [self.lstEditData.item(i).text() for i in range(self.lstEditData.count())])
            if not res:
                self.parent.error(u"오류가 발생하여 기초자료/수정성과 비교가 중단되었습니다.")
                return

            res = self.createInpsectObj(self.EDIT_SCHEMA)
            if not res:
                self.parent.error(u"오류가 발생하여 기초자료/수정성과 비교가 중단되었습니다.")
                return

            originTableList = self.selectTableList(self.ORIGIN_SCHEMA)
            editTableList = self.selectTableList(self.EDIT_SCHEMA)

            editTableList.remove("inspect_obj")

            for editTable in editTableList:
                # 비교할 테이블이 없으면 넘어감
                if editTable not in originTableList:
                    self.parent.error(u'{table}가 매칭되는 기준 데이터가 없습니다.'.format(table=editTable))
                    continue

                originGeomType = self.selectGeometryType(self.ORIGIN_SCHEMA, editTable)
                editGeomType = self.selectGeometryType(self.EDIT_SCHEMA, editTable)

                if originGeomType == '' or editGeomType == '' or originGeomType != editGeomType:
                    self.parent.error(u'{table}의 도형타입이 기준 데이터와 일치하지 않습니다.'.format(table=editTable))
                    continue

                self.selectColList(self.EDIT_SCHEMA, editTable)

                if editGeomType == 'POLYGON' or editGeomType == 'MULTIPOLYGON':  # 폴리곤 일때는 GeoHash 와 면적 생성
                    self.geohash_sql = u'st_geohash(ST_Transform(st_centroid(st_envelope(wkb_geometry)), 4326), ' \
                                       u'12) as mbr_hash_12, round( CAST(st_area(wkb_geometry) as numeric), 1) as geom_area'
                elif editGeomType == 'LINESTRING' or editGeomType == 'MULTILINESTRING':  # 선 일때는 GeoHash 와 길이 생성
                    self.geohash_sql = u'st_geohash(ST_Transform(st_centroid(st_envelope(wkb_geometry)), 4326), 12) ' \
                                       u'as mbr_hash_12, round( CAST(st_length(wkb_geometry) as numeric), 1) as geom_length'
                else:  # 점 일때는 GeoHash 만 생성
                    self.geohash_sql = u'st_geohash(ST_Transform(wkb_geometry, 4326), 12) as mbr_hash_12'

                self.lblStatus.setText(u"{} : 동일 객체 탐지".format(editTable))
                self.findSame(editTable, editGeomType)
                self.lblStatus.setText(u"{} : 형상 수정 탐지".format(editTable))
                self.findEditOnlyGeomety(editTable)
                self.lblStatus.setText(u"{} : 속성 수정 탐지".format(editTable))
                self.findEditAttr(editTable, editGeomType)
                self.lblStatus.setText(u"{} : 삭제 객체 탐지".format(editTable))
                self.findDel(editTable)
                self.lblStatus.setText(u"{} : 추가 객체 탐지".format(editTable))
                self.findAdd(editTable)

            self.lblStatus.setText(u"결과 불러오는 중 ... ")
            self.addLayers()

            self.progressMain.setMaximum(10)
            self.lblStatus.setText(u"변화내용 자동비교 완료 ")
            self.parent.info(u"변화내용 자동비교 완료 ")

        except Exception as e:
            self.lblStatus.setText(u"변화내용 자동비교 오류로 중단")
            self.parent.error(u"변화내용 자동비교 오류로 중단")
            raise e
        finally:
            QgsApplication.restoreOverrideCursor()
            self.progressMain.setMaximum(10)
            self.progressMain.setValue(0)

        self.parent.alert(u"자동비교가 완료되었으니 납품용 파일을 만들어 주세요.")

        # 변화정보를 저장할 폴더나 파일 선택
        dlg = ResSaveDialog(self.parent, self)
        rc = dlg.exec_()

        if not rc:
            return
        dlg.run_export()

    def insertData(self, schema, dataType, folder, layerList):
        if dataType == 'shp':
            return self.__insertShp(schema, folder, layerList)

        else:
            return self.__insertGpkg(schema, folder, layerList)

    def __insertShp(self, schema, folder, layerList):
        createResult = self.createSchema(schema)
        if not createResult:
            return False

        pgConnectInfo = 'PG:host={host} port={port} dbname={dbname} user={user} password={password}' \
            .format(host=self.dbHost, port=self.dbPort, dbname=self.dbNm,
                    user=self.dbUser, password=self.dbPassword)

        # 좌표계 정보 생성
        crs = osr.SpatialReference()
        crs.ImportFromEPSG(5179)

        # 대상 파일 만들기
        pg = gdal.OpenEx(pgConnectInfo, gdal.OF_VECTOR, ["PostgreSQL"], ["SCHEMA={}".format(schema), 'PRECISION=NO'])

        self.progressMain.setMaximum(len(layerList))
        j = 0
        for layerName in layerList:
            self.lblStatus.setText(u"{} 레이어 읽는 중...".format(layerName))
            j += 1
            self.progressMain.setValue(j)
            force_gui_update()

            if platform.system() == 'Windows':
                shpPath = os.path.join(folder.replace("/", "\\"), layerName) + '.shp'
            else:
                shpPath = os.path.join(folder, layerName) + '.shp'

            # 한글 코드 판단
            dbfFilePath = os.path.join(folder, layerName) + ".dbf"
            encoding = findEncoding(dbfFilePath)

            # Shape 레이어 읽기
            shp = gdal.OpenEx(shpPath, gdal.OF_VECTOR, ["ESRI Shapefile"],
                              ["SHAPE_ENCODING={}".format(encoding), "ENCODING={}".format(encoding), 'PRECISION=NO'])
            shpLayer = shp.GetLayer()

            # 원본 레이어 정보 얻기
            geomType = shpLayer.GetGeomType()
            layerDefinition = shpLayer.GetLayerDefn()

            # 원본 레이어와 동일하게 대상 레이어 만들기
            pgLayer = pg.CreateLayer("{}.{}".format(schema, layerName).encode('UTF8'), crs, geom_type=geomType)
            for i in range(layerDefinition.GetFieldCount()):
                fieldDefn = layerDefinition.GetFieldDefn(i)
                # 이 조건절이 없으면 변환시 precision 문제로 인한 overflow 오류가 난다.
                if fieldDefn.GetType() == ogr.OFTReal:
                    fieldDefn.SetWidth(0)
                    fieldDefn.SetPrecision(0)
                pgLayer.CreateField(fieldDefn)

            pgLayerDefn = pgLayer.GetLayerDefn()
            shpCnt = len(shpLayer)
            layerIdx = 0
            self.parent.prgSub.setMinimum(0)
            self.parent.prgSub.setMaximum(shpCnt)
            # 원본 레이어의 객체 돌며
            for shpFeature in shpLayer:
                # 동일한 대상 객체 생성
                pgFeature = ogr.Feature(pgLayerDefn)
                for i in range(pgLayerDefn.GetFieldCount()):
                    pgFeature.SetField(pgLayerDefn.GetFieldDefn(i).GetNameRef(), shpFeature.GetField(i))
                geom = shpFeature.GetGeometryRef()
                pgFeature.SetGeometry(geom)
                pgLayer.CreateFeature(pgFeature)
                layerIdx = layerIdx + 1
                self.parent.prgSub.setValue(layerIdx)
                force_gui_update()

            sql = u"CREATE INDEX ON {}.{} USING GIST (wkb_geometry)".format(schema, layerName)
            pg.ExecuteSQL(sql.encode("UTF8"))

        return True

    def __insertGpkg(self, schema, filePath, layerList):
        self.parent.info(u"{} 파일을 읽고 있습니다.".format(filePath))

        createResult = self.createSchema(schema)
        if not createResult:
            return

        pgConnectInfo = 'PG:host={host} port={port} dbname={dbname} user={user} password={password}' \
            .format(host=self.dbHost, port=self.dbPort, dbname=self.dbNm,
                    user=self.dbUser, password=self.dbPassword)

        # 좌표계 정보 생성
        crs = osr.SpatialReference()
        crs.ImportFromEPSG(5179)

        pg = gdal.OpenEx(pgConnectInfo, gdal.OF_VECTOR, ["PostgreSQL"], ["SCHEMA={}".format(schema), 'PRECISION=NO'])
        gpkg = gdal.OpenEx(filePath, gdal.OF_VECTOR, ["GPKG"])

        j = 0
        self.progressMain.setMaximum(len(layerList))
        for layerName in layerList:
            self.lblStatus.setText(u"{} 레이어 읽는 중...".format(layerName))

            j += 1
            self.progressMain.setValue(j)
            force_gui_update()

            gpkgLayer = gpkg.GetLayer(layerName.encode("UTF8"))

            # 원본 레이어 정보 얻기
            geomType = gpkgLayer.GetGeomType()
            layerDefinition = gpkgLayer.GetLayerDefn()

            # 원본 레이어와 동일하게 대상 레이어 만들기
            pgLayer = pg.CreateLayer("{}.{}".format(schema, layerName).encode('UTF8'), crs, geom_type=geomType)
            for i in range(layerDefinition.GetFieldCount()):
                fieldDefn = layerDefinition.GetFieldDefn(i)
                # 이 조건절이 없으면 변환시 precision 문제로 인한 overflow 오류가 난다.
                if fieldDefn.GetType() == ogr.OFTReal:
                    fieldDefn.SetWidth(0)
                    fieldDefn.SetPrecision(0)
                pgLayer.CreateField(fieldDefn)

            pgLayerDefn = pgLayer.GetLayerDefn()
            entCount = len(gpkgLayer)
            layerIdx = 0
            self.parent.prgSub.setMinimum(0)
            self.parent.prgSub.setMaximum(entCount)
            # 원본 레이어의 객체 돌며
            for shpFeature in gpkgLayer:
                # 동일한 대상 객체 생성
                pgFeature = ogr.Feature(pgLayerDefn)
                for i in range(pgLayerDefn.GetFieldCount()):
                    pgFeature.SetField(pgLayerDefn.GetFieldDefn(i).GetNameRef(), shpFeature.GetField(i))
                geom = shpFeature.GetGeometryRef()
                pgFeature.SetGeometry(geom)
                pgLayer.CreateFeature(pgFeature)
                layerIdx = layerIdx + 1
                self.parent.prgSub.setValue(layerIdx)
                force_gui_update()

            sql = u"CREATE INDEX ON {}.{} USING GIST (wkb_geometry)".format(schema, layerName)
            pg.ExecuteSQL(sql.encode("UTF8"))

        return True

    # 기본 SQL
    def selectSchemaList(self):
        schemaList = list()

        cur = self.conn.cursor()

        try:
            selectSql = self.sqlStatement.get("SQL", "selectSchemaList")
            cur.execute(SQL(selectSql), {"user": self.dbUser})

            sqlResults = cur.fetchall()

            for sqlResult in sqlResults:
                schemaList.append(sqlResult[0])

        except Exception as e:
            self.parent.error(u"스키마 리스트를 조회하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

        if cur:
            cur.close()

        return schemaList

    def createSchema(self, schema):
        result = False

        cur = self.conn.cursor()

        try:
            createSql = self.sqlStatement.get("SQL", "createSchema")
            self.parent.debug(cur.mogrify(SQL(createSql).format(schema=Identifier(schema))))
            cur.execute(SQL(createSql).format(schema=Identifier(schema)))

            self.conn.commit()

            result = True

        except Exception as e:
            self.parent.error(u"스키마를 생성하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

            self.conn.rollback()

        if cur:
            cur.close()

        return result

    def dropSchema(self, schema):
        result = False
        errMsg = None

        cur = self.conn.cursor()

        try:
            dropSql = self.sqlStatement.get("SQL", "dropSchema")
            cur.execute(SQL(dropSql).format(schema=Identifier(schema)))

            self.conn.commit()

            result = True

        except Exception as e:
            self.parent.error(u"스키마를 삭제하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

            self.conn.rollback()

        if cur:
            cur.close()

        return result, errMsg

    def selectTableList(self, schema):
        tableList = list()

        cur = self.conn.cursor()

        try:
            selectSql = self.sqlStatement.get("SQL", "selectTableList")
            cur.execute(selectSql, {"schema": schema})
            sqlResults = cur.fetchall()

            for sqlResult in sqlResults:
                tableList.append(sqlResult[0])

        except Exception as e:
            self.parent.error(u"테이블 리스트를 조회하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

        if cur:
            cur.close()

        return tableList

    def selectGeometryType(self, schema, table):
        geomType = str()

        cur = self.conn.cursor()

        try:
            selectSql = """
                SELECT GeometryType(wkb_geometry) FROM {schema}.{table} limit 1;
            """

            cur.execute(SQL(selectSql).format(schema=Identifier(schema),
                                              table=Identifier(table)))
            sqlResult = cur.fetchone()

            geomType = sqlResult[0]

        except Exception as e:
            self.parent.error(u"지오메트리 타입을 조회하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

        if cur:
            cur.close()

        return geomType

    def selectColList(self, schema, table, checkNum=True):
        all_column_nm = []
        num_column_nm = []

        cur = self.conn.cursor()

        sql = """
                select column_name from information_schema.columns
                where table_schema = '{schema}' and table_name = '{table}' order by ordinal_position asc
            """.format(schema=schema, table=table)
        cur.execute(sql)
        all_results = cur.fetchall()

        if checkNum:
            sql = """
                    select column_name from information_schema.columns 
                    where table_schema = '{schema}' and table_name = '{table}' and data_type = 'numeric';
                """.format(schema=schema, table=table)

            cur.execute(sql)
            num_results = cur.fetchall()

            for list in num_results:
                num_column_nm.append(list[0])

            for list in all_results:
                if list[0] in num_column_nm:
                    all_column_nm.append(u"round({0}, 2) as {0}".format(list[0]))
                else:
                    all_column_nm.append(list[0])
        else:
            for list in all_results:
                all_column_nm.append(list[0])

        removeColList = ['ogc_fid', 'fid', 'wkb_geometry', 'obchg_dt', 'rsreg_de', 'obchg_se', 'mnent_nm',
                         'inins_se', 'lcins_se', 'loins_se', 'tiins_se', 'thins_se', 'dbreg_dt']

        for removeCol in removeColList:
            if removeCol in all_column_nm:
                all_column_nm.remove(removeCol)

        self.column_sql = ','.join(all_column_nm)

        self.id_column = all_column_nm[0]

    #########
    """
    분류코드, 분류코드명, No, 코드ID, 코드명
    OCS_CODE, 객체변동구분, 1, OCS001, 추가
        , 객체변동구분, 2, OCS002, 삭제
        , 객체변동구분, 3, OCS003, 형상수정
        , 객체변동구분, 4, OCS004, 속성수정
        , 객체변동구분, 5, OCS005, 형상/속성수정
    """
    """
    mod_type
    a, r, s, eg, ef
    """

    # 변화 탐지 SQL
    # 변화 정보 테이블
    def createInpsectObj(self, schema):
        result = False

        cur = self.conn.cursor()

        try:
            createSql = """
                    CREATE TABLE {schema}.inspect_obj (
                        seq_no serial primary key,
                        layer_nm character varying(50),
                        origin_ogc_fid integer,
                        receive_ogc_fid integer,
                        mod_type character varying(3)
                    );
                """

            cur.execute(SQL(createSql).format(schema=Identifier(schema)))

            self.conn.commit()

            result = True

        except Exception as e:
            self.parent.error(u"지오메트리 타입을 조회하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

            self.conn.rollback()

        if cur:
            cur.close()

        return result

    # 동일 파일 검사
    def findSame(self, table, geom_type):
        self.parent.info(u"{}의 동일 객체를 탐지하고 있습니다.".format(table))
        cur = self.conn.cursor()

        if geom_type == 'POLYGON' or geom_type == 'MULTIPOLYGON':  # 폴리곤 일때는 면적 비교 추가
            add_sql = u'and e.geom_area between o.geom_area*0.95 and o.geom_area*1.05'
        elif geom_type == 'LINESTRING' or geom_type == 'MULTILINESTRING':  # 선 일때는 길이 비교 추가
            add_sql = u'and e.geom_length between o.geom_length*0.95 and o.geom_length*1.05'
        else:  # 점 일때는 PK 비교 추가
            add_sql = u'and o.{0} = e.{0}'.format(self.id_column)

        try:

            insertSql = """
                    WITH geom_same_data AS (
                        SELECT o.* FROM (
                            SELECT {column_sql}, {geohash_sql}
                            FROM {origin_schema}.{table} 
                        ) AS o
                        INNER JOIN (
                            SELECT {column_sql}, {geohash_sql}
                            FROM {edit_schema}.{table}
                        ) AS e
                        ON o.mbr_hash_12 = e.mbr_hash_12 
                        {add_sql}
                        AND o.{id_col} = e.{id_col}
                    )
                    , same_data AS (
                        SELECT o.* FROM (
                            SELECT {column_sql}, mbr_hash_12 FROM geom_same_data
                        ) AS o
                        INNER JOIN (
                            SELECT {column_sql}, 
                            ST_Geohash(ST_Transform(ST_Centroid(ST_Envelope(wkb_geometry)), 4326), 12) AS mbr_hash_12
                            FROM {edit_schema}.{table}
                        ) AS e
                        ON (o.*) = (e.*)
                    )
                    , origin AS (
                        SELECT o.ogc_fid AS origin_ogc_fid, a.{id_col}, a.mbr_hash_12
                        FROM same_data AS a
                        INNER JOIN (
                            SELECT ogc_fid, {id_col}, 
                            ST_Geohash(ST_Transform(ST_Centroid(ST_Envelope(wkb_geometry)), 4326), 12) AS mbr_hash_12
                            FROM {origin_schema}.{table}
                        ) AS o
                        ON a.{id_col} = o.{id_col}
                        AND a.mbr_hash_12 = o.mbr_hash_12
                    )
                    , recevie AS (
                        SELECT o.ogc_fid AS receive_ogc_fid, a.{id_col}, a.mbr_hash_12
                        FROM same_data AS a
                        INNER JOIN (
                            SELECT ogc_fid, {id_col}, 
                            ST_Geohash(ST_Transform(ST_Centroid(ST_Envelope(wkb_geometry)), 4326), 12) AS mbr_hash_12
                            FROM {edit_schema}.{table}
                        ) AS o
                        ON a.{id_col} = o.{id_col}
                        AND a.mbr_hash_12 = o.mbr_hash_12
                    )
                    INSERT INTO {edit_schema}.inspect_obj(
                        layer_nm, origin_ogc_fid, receive_ogc_fid, mod_type
                    )
                    SELECT %(layer_nm)s, origin_ogc_fid, receive_ogc_fid, 's'
                    FROM origin, recevie
                    WHERE origin.{id_col} = recevie.{id_col}
                    AND origin.mbr_hash_12 = recevie.mbr_hash_12;
                """
            threadExecuteSql(cur, SQL(insertSql).format(column_sql=SQL(self.column_sql),
                                                        geohash_sql=SQL(self.geohash_sql),
                                                        origin_schema=Identifier(self.ORIGIN_SCHEMA),
                                                        edit_schema=Identifier(self.EDIT_SCHEMA),
                                                        table=Identifier(table),
                                                        add_sql=SQL(add_sql),
                                                        id_col=Identifier(self.id_column)),
                             {"layer_nm": table})

        except Exception as e:
            self.parent.error(u"스키마 리스트를 조회하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

            self.conn.rollback()

        else:
            self.conn.commit()

    # 형상 수정
    def findEditOnlyGeomety(self, table):
        self.parent.info(u"{}의 형상 수정 객체를 탐지하고 있습니다.".format(table))
        cur = self.conn.cursor()

        try:

            insertSql = """
                    WITH attr_same_data AS (
                        SELECT o.* 
                        FROM (
                            SELECT {column_sql} FROM {origin_schema}.{table}
                        ) AS o, 
                        (
                            SELECT {column_sql} FROM {edit_schema}.{table}
                        ) AS e
                        WHERE (o.*) = (e.*)
                    )
                    , same_data AS (
                        SELECT wkb_geometry, {column_sql} 
                        FROM {origin_schema}.{table} 
                        WHERE ogc_fid IN (
                            SELECT origin_ogc_fid FROM {edit_schema}.inspect_obj
                            WHERE mod_type = 's'
                            AND layer_nm = %(layer_nm)s
                        )
                    )
                    , join_geom AS (
                        SELECT o.wkb_geometry, a.* 
                        FROM attr_same_data AS a, {origin_schema}.{table} AS o
                        WHERE o.{id_col} = a.{id_col}
                    )
                    , geo_edit AS (
                        SELECT * FROM join_geom
                        EXCEPT
                        SELECT * FROM same_data
                    )
                    INSERT INTO {edit_schema}.inspect_obj(layer_nm, origin_ogc_fid, receive_ogc_fid, mod_type)
                    SELECT %(layer_nm)s, origin_ogc_fid, receive_ogc_fid, 'eg'
                    FROM (
                        SELECT ogc_fid AS origin_ogc_fid, e.{id_col} 
                        FROM geo_edit AS e
                        INNER JOIN {origin_schema}.{table} AS o
                        ON e.{id_col} = o.{id_col}
                    ) AS origin
                    , (
                        SELECT ogc_fid AS receive_ogc_fid, e.{id_col} 
                        FROM geo_edit AS e
                        INNER JOIN {edit_schema}.{table} AS o
                        ON e.{id_col} = o.{id_col}
                    ) AS edit
                    WHERE origin.{id_col} = edit.{id_col};
                """

            threadExecuteSql(cur, SQL(insertSql).format(column_sql=SQL(self.column_sql),
                                                        origin_schema=Identifier(self.ORIGIN_SCHEMA),
                                                        edit_schema=Identifier(self.EDIT_SCHEMA),
                                                        table=Identifier(table),
                                                        id_col=Identifier(self.id_column)),
                             {"layer_nm": table})

        except Exception as e:
            self.parent.error(u"스키마 리스트를 조회하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

            self.conn.rollback()

        else:
            self.conn.commit()

    def findEditAttr(self, table, geom_type):
        self.parent.info(u"{}의 속성 수정 객체를 탐지하고 있습니다.".format(table))
        cur = self.conn.cursor()

        try:

            if geom_type == 'POLYGON' or geom_type == 'MULTIPOLYGON':
                insertSql = """
                        WITH same AS (
                            SELECT {column_sql}, {geohash_sql} 
                            FROM {origin_schema}.{table}
                            WHERE ogc_fid IN (
                                SELECT origin_ogc_fid 
                                FROM {edit_schema}.inspect_obj
                                where mod_type = 's'
                                AND layer_nm = %(layer_nm)s
                            )
                        )
                        , om AS (
                            SELECT {column_sql}, {geohash_sql} 
                            FROM {origin_schema}.{table} 
                            EXCEPT
                            SELECT * FROM same 
                        )
                        , em AS (
                            SELECT {column_sql}, {geohash_sql} 
                            FROM {edit_schema}.{table} 
                            EXCEPT
                            SELECT * FROM same 
                        )
                        , geometry AS ( 
                            SELECT mbr_hash_12, geom_area 
                            FROM ( 
                                SELECT om.* FROM om 
                                INNER join em 
                                ON em.mbr_hash_12 = om.mbr_hash_12 
                                AND em.geom_area BETWEEN om.geom_area*0.95 
                                AND om.geom_area*1.05 
                            ) AS t
                        ) 
                        INSERT INTO {edit_schema}.inspect_obj(layer_nm, origin_ogc_fid,receive_ogc_fid,mod_type)
                        SELECT %(layer_nm)s, origin_ogc_fid, receive_ogc_fid, 'ef' as mod_type 
                        FROM (
                            SELECT ogc_fid AS origin_ogc_fid, o.mbr_hash_12, o.geom_area 
                            FROM geometry 
                            INNER JOIN (
                                SELECT ogc_fid, {geohash_sql} 
                                FROM {origin_schema}.{table} 
                            ) as o 
                            ON geometry.mbr_hash_12=o.mbr_hash_12 
                            AND geometry.geom_area BETWEEN o.geom_area*0.95 AND o.geom_area*1.05
                        ) AS origin, 
                        (
                            SELECT ogc_fid AS receive_ogc_fid, o.mbr_hash_12, o.geom_area 
                            FROM geometry 
                            INNER JOIN (
                                SELECT ogc_fid, {geohash_sql} 
                                FROM {edit_schema}.{table} 
                            ) as o 
                            ON geometry.mbr_hash_12=o.mbr_hash_12 
                            AND geometry.geom_area BETWEEN o.geom_area*0.95 AND o.geom_area*1.05
                        ) as receive 
                        WHERE receive.mbr_hash_12=origin.mbr_hash_12 
                        AND receive.geom_area BETWEEN origin.geom_area*0.95 AND origin.geom_area*1.05
                    """
            elif geom_type == 'LINESTRING' or geom_type == 'MULTILINESTRING':
                insertSql = """
                        WITH same AS (
                            SELECT {column_sql}, {geohash_sql} 
                            FROM {origin_schema}.{table}
                            WHERE ogc_fid IN (
                                SELECT origin_ogc_fid 
                                FROM {edit_schema}.inspect_obj
                                where mod_type = 's'
                                AND layer_nm = %(layer_nm)s
                            )
                        )
                        , om AS (
                            SELECT {column_sql}, {geohash_sql} 
                            FROM {origin_schema}.{table} 
                            EXCEPT
                            SELECT * FROM same 
                        )
                        , em AS (
                            SELECT {column_sql}, {geohash_sql} 
                            FROM {edit_schema}.{table} 
                            EXCEPT
                            SELECT * FROM same 
                        )
                        , geometry AS ( 
                            SELECT mbr_hash_12, geom_length 
                            FROM ( 
                                SELECT om.* FROM om 
                                INNER join em 
                                ON em.mbr_hash_12 = om.mbr_hash_12 
                                AND em.geom_length BETWEEN om.geom_length*0.95 
                                AND om.geom_length*1.05 
                            ) AS t
                        ) 
                        INSERT INTO {edit_schema}.inspect_obj(layer_nm, origin_ogc_fid,receive_ogc_fid,mod_type)
                        SELECT %(layer_nm)s, origin_ogc_fid, receive_ogc_fid, 'ef' as mod_type 
                        FROM (
                            SELECT ogc_fid AS origin_ogc_fid, o.mbr_hash_12, o.geom_length 
                            FROM geometry 
                            INNER JOIN (
                                SELECT ogc_fid, {geohash_sql} 
                                FROM {origin_schema}.{table} 
                            ) as o 
                            ON geometry.mbr_hash_12=o.mbr_hash_12 
                            AND geometry.geom_length BETWEEN o.geom_length*0.95 AND o.geom_length*1.05
                        ) AS origin, 
                        (
                            SELECT ogc_fid AS receive_ogc_fid, o.mbr_hash_12, o.geom_length 
                            FROM geometry 
                            INNER JOIN (
                                SELECT ogc_fid, {geohash_sql} 
                                FROM {edit_schema}.{table} 
                            ) as o 
                            ON geometry.mbr_hash_12=o.mbr_hash_12 
                            AND geometry.geom_length BETWEEN o.geom_length*0.95 AND o.geom_length*1.05
                        ) as receive 
                        WHERE receive.mbr_hash_12=origin.mbr_hash_12 
                        AND receive.geom_length BETWEEN origin.geom_length*0.95 AND origin.geom_length*1.05
                    """
            else:
                insertSql = """
                        WITH same AS (
                            SELECT {column_sql}, {geohash_sql} 
                            FROM {origin_schema}.{table}
                            WHERE ogc_fid IN (
                                SELECT origin_ogc_fid 
                                FROM {edit_schema}.inspect_obj
                                where mod_type = 's'
                                AND layer_nm = %(layer_nm)s
                            )
                        )
                        , om AS (
                            SELECT {column_sql}, {geohash_sql} 
                            FROM {origin_schema}.{table} 
                            EXCEPT 
                            SELECT * FROM same 
                        )
                        , em AS (
                            SELECT {column_sql}, {geohash_sql} 
                            FROM {edit_schema}.{table} 
                            EXCEPT 
                            SELECT * FROM same 
                        )
                        , geometry AS ( 
                            SELECT mbr_hash_12 
                            FROM ( 
                                SELECT om.* FROM om 
                                INNER JOIN em 
                                ON em.mbr_hash_12 = om.mbr_hash_12 
                            ) AS t
                        ) 
                        INSERT INTO {edit_schema}.inspect_obj(layer_nm, origin_ogc_fid,receive_ogc_fid,mod_type)
                        SELECT %(layer_nm)s, origin_ogc_fid, receive_ogc_fid, 'ef' AS mod_type 
                        FROM (
                            SELECT ogc_fid AS origin_ogc_fid, o.mbr_hash_12 
                            FROM geometry 
                            INNER JOIN (
                                SELECT ogc_fid, {geohash_sql} 
                                FROM {origin_schema}.{table} 
                            ) AS o 
                            ON geometry.mbr_hash_12= o.mbr_hash_12) AS origin
                            , (
                                SELECT ogc_fid AS receive_ogc_fid, o.mbr_hash_12 
                                FROM geometry 
                                INNER JOIN (
                                    SELECT ogc_fid, {geohash_sql} 
                                    FROM {edit_schema}.{table} 
                                ) AS o 
                                ON geometry.mbr_hash_12 = o.mbr_hash_12
                            ) AS receive 
                            WHERE origin.mbr_hash_12 = receive.mbr_hash_12
                    """
            threadExecuteSql(cur, SQL(insertSql).format(column_sql=SQL(self.column_sql),
                                                        geohash_sql=SQL(self.geohash_sql),
                                                        origin_schema=Identifier(self.ORIGIN_SCHEMA),
                                                        edit_schema=Identifier(self.EDIT_SCHEMA),
                                                        table=Identifier(table),
                                                        id_col=Identifier(self.id_column)),
                             {"layer_nm": table})

        except Exception as e:
            self.parent.error(u"스키마 리스트를 조회하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

            self.conn.rollback()

        else:
            self.conn.commit()

    def findDel(self, table):
        self.parent.info(u"{}의 삭제 객체를 탐지하고 있습니다.".format(table))
        cur = self.conn.cursor()

        try:

            insertSql = """
                    INSERT INTO {edit_schema}.inspect_obj(layer_nm, origin_ogc_fid, receive_ogc_fid, mod_type) 
                    SELECT %(layer_nm)s, origin_ogc_fid, 0 AS receive_ogc_fid, 'r' AS mod_type 
                    FROM (
                        SELECT ogc_fid AS origin_ogc_fid 
                        FROM {origin_schema}.{table} 
                        EXCEPT 
                        SELECT origin_ogc_fid 
                        FROM {edit_schema}.inspect_obj
                        WHERE layer_nm = %(layer_nm)s
                    ) AS rm
                """
            threadExecuteSql(cur, SQL(insertSql).format(column_sql=SQL(self.column_sql),
                                                        origin_schema=Identifier(self.ORIGIN_SCHEMA),
                                                        edit_schema=Identifier(self.EDIT_SCHEMA),
                                                        table=Identifier(table)),
                             {"layer_nm": table})

        except Exception as e:
            self.parent.error(u"스키마 리스트를 조회하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

            self.conn.rollback()

        else:
            self.conn.commit()

    def findAdd(self, table):
        self.parent.info(u"{}의 추가 객체를 탐지하고 있습니다.".format(table))
        cur = self.conn.cursor()

        try:

            insertSql = """
                    INSERT INTO {edit_schema}.inspect_obj(layer_nm, origin_ogc_fid, receive_ogc_fid, mod_type )
                    SELECT %(layer_nm)s, 0 as origin_ogc_fid, receive_ogc_fid, 'a' as mod_type 
                    FROM (
                        SELECT ogc_fid AS receive_ogc_fid
                        FROM {edit_schema}.{table}
                        EXCEPT
                        SELECT receive_ogc_fid
                        FROM {edit_schema}.inspect_obj
                        WHERE layer_nm = %(layer_nm)s
                    ) AS add
                """
            threadExecuteSql(cur, SQL(insertSql).format(column_sql=SQL(self.column_sql),
                                                        origin_schema=Identifier(self.ORIGIN_SCHEMA),
                                                        edit_schema=Identifier(self.EDIT_SCHEMA),
                                                        table=Identifier(table),
                                                        id_col=Identifier(self.id_column)),
                             {"layer_nm": table})

        except Exception as e:
            self.parent.error(u"스키마 리스트를 조회하는 도중 문제가 발생하였습니다.")
            self.parent.error(e)

            self.conn.rollback()

        else:
            self.conn.commit()

    def addLayers(self):
        self.parent.info(u"탐지 결과를 불러오고 있습니다.")
        QgsMapLayerRegistry.instance().removeAllMapLayers()

        canvas = self.iface.mapCanvas()
        canvas.mapRenderer().setProjectionsEnabled(True)
        canvas.mapRenderer().setDestinationCrs(QgsCoordinateReferenceSystem(5179))

        tableList = self.selectTableList(self.EDIT_SCHEMA)
        if 'inspect_obj' in tableList:
            tableList.remove('inspect_obj')

        for table in tableList:

            self.selectColList(self.EDIT_SCHEMA, table, False)

            uri = QgsDataSourceURI()

            # 기존의 데이터 ( 변화없는 정보 )
            sql = "(SELECT row_number() over (order by mod_type asc) as id, wkb_geometry, {column_sql}, b.mod_type " \
                  "FROM {origin_schema}.{table} AS a, {edit_schema}.inspect_obj AS b " \
                  "WHERE a.ogc_fid = b.origin_ogc_fid " \
                  "AND layer_nm = '{table}' " \
                  "AND mod_type != 'r')".format(column_sql=self.column_sql,
                                                origin_schema=self.ORIGIN_SCHEMA,
                                                edit_schema=self.EDIT_SCHEMA,
                                                table=table)

            uri.setConnection(self.dbHost, self.dbPort, self.dbNm, self.dbUser, self.dbPassword)
            uri.setDataSource("", sql, "wkb_geometry", "", "id")
            self.maintain_data = QgsVectorLayer(uri.uri(), u'변화없음_' + table, "postgres")

            symbol = None
            if self.maintain_data.wkbType() == QGis.WKBPolygon or self.maintain_data.wkbType() == QGis.WKBMultiPolygon:
                symbol = QgsFillSymbolV2().createSimple({'color_border': '#a7a7a7', 'width_border': '0.5',
                                                         'style': 'no', 'style_border': 'solid',
                                                         "outline_width_unit": "pixel"})
            elif self.maintain_data.wkbType() == QGis.WKBLineString or self.maintain_data.wkbType() == QGis.WKBMultiLineString:
                symbol = QgsLineSymbolV2().createSimple({'color': '#a7a7a7', 'width': '0.5',
                                                         'style': 'solid',
                                                         "outline_width_unit": "pixel"})
            else:
                symbol = QgsMarkerSymbolV2.createSimple({'name': 'circle', 'color': '#a7a7a7', 'size': '1',
                                                         'outline_style': 'no',
                                                         "size_unit": "pixel"})

            self.maintain_data.rendererV2().setSymbol(symbol)
            QgsMapLayerRegistry.instance().addMapLayer(self.maintain_data)

            # 변화가 있는 정보
            sql = "(SELECT row_number() over (order by mod_type asc) as id, * " \
                  "FROM ( SELECT wkb_geometry, {column_sql}, b.mod_type " \
                  "FROM {origin_schema}.{table} AS a, {edit_schema}.inspect_obj AS b " \
                  "WHERE a.ogc_fid = b.origin_ogc_fid " \
                  "AND layer_nm = '{table}' " \
                  "AND mod_type = 'r' " \
                  "UNION SELECT wkb_geometry, {column_sql}, b.mod_type " \
                  "FROM {edit_schema}.{table} AS a, {edit_schema}.inspect_obj AS b " \
                  "WHERE a.ogc_fid = b.receive_ogc_fid " \
                  "AND layer_nm = '{table}' " \
                  "AND mod_type != 's') AS foo)".format(column_sql=self.column_sql,
                                                        origin_schema=self.ORIGIN_SCHEMA,
                                                        edit_schema=self.EDIT_SCHEMA,
                                                        table=table)

            uri.setDataSource("", sql, "wkb_geometry", "", "id")
            self.diff_data = QgsVectorLayer(uri.uri(), u'변화정보_' + table, "postgres")

            mod_type_symbol = {
                'a': ('green', u'추가'),
                'r': ('red', u'삭제'),
                'eg': ('orange', u'도형변경'),
                'ef': ('blue', u'속성변경')
            }

            diff_data_type = self.diff_data.wkbType()
            categories = []
            for mod_type, (color, label) in mod_type_symbol.items():
                if diff_data_type == QGis.WKBPolygon or diff_data_type == QGis.WKBMultiPolygon:
                    symbol = QgsFillSymbolV2().createSimple({'color_border': color, 'width_border': '1.5',
                                                             'style': 'no', 'style_border': 'solid',
                                                             "outline_width_unit": "pixel"})
                elif diff_data_type == QGis.WKBLineString or diff_data_type == QGis.WKBMultiLineString:
                    symbol = QgsLineSymbolV2().createSimple({'color': color, 'width': '1.5',
                                                             'style': 'solid',
                                                             "outline_width_unit": "pixel"})
                else:
                    symbol = QgsMarkerSymbolV2.createSimple({'name': 'circle', 'color': color, 'size': '3',
                                                             'outline_style': 'no',
                                                             "size_unit": "pixel"})
                category = QgsRendererCategoryV2(mod_type, symbol, label)
                categories.append(category)

            expression = 'mod_type'  # field name
            renderer = QgsCategorizedSymbolRendererV2(expression, categories)
            self.diff_data.setRendererV2(renderer)

            QgsMapLayerRegistry.instance().addMapLayer(self.diff_data)

            self.inspectList = []
            self.crrIndex = -1
            iter = self.diff_data.getFeatures()
            for feature in iter:
                self.inspectList.append(feature)

            self.numTotal = len(self.inspectList)

            # 변경된 데이터가 없을 경우
            if self.numTotal <= 0:
                canvas.setExtent(self.maintain_data.extent())
                canvas.refresh()
                self.insertInspectRes()
                return
