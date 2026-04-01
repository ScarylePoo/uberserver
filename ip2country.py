#!/usr/bin/python3
# coding=utf-8
# This file is part of the uberserver (GPL v2 or later), see LICENSE
#
# Patched to use geoip2 + MaxMind GeoLite2 database instead of the
# legacy GeoIP C-extension (which is incompatible with Ubuntu 24.04).
#
# The GeoLite2-Country.mmdb file is downloaded automatically at container
# build time by the Docker entrypoint, and placed at the path below.
# You can also download it manually from:
#   https://dev.maxmind.com/geoip/geolite2-free-geolocation-data
# (free account required)

dbfile = "/usr/share/GeoIP/GeoLite2-Country.mmdb"

def loaddb():
	global _reader
	try:
		import geoip2.database
		_reader = geoip2.database.Reader(dbfile)
		return True
	except Exception as e:
		print("Couldn't load %s: %s" % (dbfile, str(e)))
		print("Hint: make sure GeoLite2-Country.mmdb is at %s" % dbfile)
		return False

working = loaddb()

def lookup(ip):
	if not working: return '??'
	try:
		response = _reader.country(ip)
		code = response.country.iso_code
		if not code: return '??'
		return code
	except Exception:
		return '??'

def reloaddb():
	global working
	working = loaddb()


if __name__ == '__main__':
	assert(lookup("37.187.59.77")  == 'FR')
	assert(lookup("77.64.139.108") == 'DE')
	assert(lookup("78.46.100.157") == 'DE')
	assert(lookup("8.8.8.8")       == 'US')
	assert(lookup("0.0.0.0")       == '??')
	print("Test ok!")
