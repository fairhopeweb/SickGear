# Author: Nyaran <nyayukko@gmail.com>, based on Antoine Bertin <diaoulael@gmail.com> work
#
# This file is part of SickGear.
#
# SickGear is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SickGear is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SickGear.  If not, see <http://www.gnu.org/licenses/>.

import datetime

from . import db, helpers, logger
from .common import *

import sickgear

from lib import subliminal

SINGLE = 'und'


def sorted_service_list():
    services_mapping = dict([(x.lower(), x) for x in subliminal.core.SERVICES])

    new_list = []

    # add all services in the priority list, in order
    cur_index = 0
    for cur_service in sickgear.SUBTITLES_SERVICES_LIST:
        if cur_service in services_mapping:
            cur_service_dict = dict(
                id=cur_service,
                image=cur_service + '.png',
                name=services_mapping[cur_service],
                enabled=1 == sickgear.SUBTITLES_SERVICES_ENABLED[cur_index],
                api_based=__import__('lib.subliminal.services.' + cur_service, globals=globals(),
                                     locals=locals(), fromlist=['Service']).Service.api_based,
                url=__import__('lib.subliminal.services.' + cur_service, globals=globals(),
                               locals=locals(), fromlist=['Service']).Service.site_url)
            new_list.append(cur_service_dict)
        cur_index += 1

    # add any services that are missing from that list
    for cur_service in services_mapping:
        if cur_service not in [x['id'] for x in new_list]:
            cur_service_dict = dict(
                id=cur_service,
                image=cur_service + '.png',
                name=services_mapping[cur_service],
                enabled=False,
                api_based=__import__('lib.subliminal.services.' + cur_service, globals=globals(),
                                     locals=locals(), fromlist=['Service']).Service.api_based,
                url=__import__('lib.subliminal.services.' + cur_service, globals=globals(),
                               locals=locals(), fromlist=['Service']).Service.site_url)
            new_list.append(cur_service_dict)

    return new_list


def get_enabled_service_list():
    return [x['name'] for x in sorted_service_list() if x['enabled']]


def is_valid_language(language):
    return subliminal.language.language_list(language)


def get_language_name(select_lang):
    return subliminal.language.Language(select_lang).name


def wanted_languages(sql_like=False):
    wanted_langs = sorted(sickgear.SUBTITLES_LANGUAGES)
    if sql_like:
        return '%' + ','.join(wanted_langs) + '%'
    return wanted_langs


def subtitles_languages(video_path):
    """Return a list detected subtitles for the given video file"""
    video = subliminal.videos.Video.from_path(video_path)
    video.subtitle_path = sickgear.SUBTITLES_DIR
    subtitles = video.scan()
    languages = set()
    for subtitle in subtitles:
        if subtitle.language:
            languages.add(subtitle.language.alpha2)
        else:
            languages.add(SINGLE)
    return list(languages)


# Return a list with languages that have alpha2 code
def subtitle_language_filter():
    return [language for language in subliminal.language.LANGUAGES if language[2] != ""]


class SubtitlesFinder(object):
    """
    The SubtitlesFinder will be executed every hour but will not necessarily search
    and download subtitles. Only if the defined rule is true
    """

    def __init__(self):
        self.amActive = False

    @staticmethod
    def is_enabled():
        return sickgear.USE_SUBTITLES

    def run(self):
        if self.is_enabled():
            self.amActive = True
            self._main()
            self.amActive = False

    def _main(self):
        if 1 > len(sickgear.subtitles.get_enabled_service_list()):
            logger.error('Not enough services selected. At least 1 service is required to'
                         ' search subtitles in the background')
            return

        logger.log('Checking for subtitles')

        # get episodes on which we want subtitles
        # criteria is:
        #  - show subtitles = 1
        #  - episode subtitles != config wanted languages or SINGLE (depends on config multi)
        #  - search count < 2 and diff(airdate, now) > 1 week : now -> 1d
        #  - search count < 7 and diff(airdate, now) <= 1 week : now -> 4h -> 8h -> 16h -> 1d -> 1d -> 1d

        today = datetime.date.today().toordinal()

        # you have 5 minutes to understand that one. Good luck
        my_db = db.DBConnection()
        sql_result = my_db.select(
            'SELECT s.show_name, e.indexer AS tv_id, e.showid AS prod_id,'
            ' e.season, e.episode, e.status, e.subtitles,'
            ' e.subtitles_searchcount AS searchcount, e.subtitles_lastsearch AS lastsearch,'
            ' e.location, (? - e.airdate) AS airdate_daydiff'
            ' FROM tv_episodes AS e'
            ' INNER JOIN tv_shows AS s'
            ' ON (e.indexer = s.indexer AND e.showid = s.indexer_id)'
            ' WHERE s.subtitles = 1 AND e.subtitles NOT LIKE (?)'
            ' AND ((e.subtitles_searchcount <= 2 AND (? - e.airdate) > 7)'
            ' OR (e.subtitles_searchcount <= 7 AND (? - e.airdate) <= 7))'
            ' AND (e.status IN (%s)' % ','.join([str(x) for x in Quality.DOWNLOADED])
            + ' OR (e.status IN (%s)' % ','.join([str(x) for x in Quality.SNATCHED + Quality.SNATCHED_PROPER])
            + ' AND e.location != \'\'))', [today, wanted_languages(True), today, today])
        if 0 == len(sql_result):
            logger.log('No subtitles to download', logger.MESSAGE)
            return

        rules = self._get_rules()
        now = datetime.datetime.now()
        for cur_result in sql_result:

            if not os.path.isfile(cur_result['location']):
                logger.debug(f'Episode file does not exist, cannot download subtitles for episode'
                             f' {cur_result["season"]:d}x{cur_result["episode"]:d} of show {cur_result["show_name"]}')
                continue

            # Old shows rule
            _ = datetime.datetime.strptime('20110101', '%Y%m%d')
            if ((cur_result['airdate_daydiff'] > 7 and cur_result['searchcount'] < 2
                 and now - datetime.datetime.strptime(cur_result['lastsearch'], '%Y-%m-%d %H:%M:%S')
                 > datetime.timedelta(hours=rules['old'][cur_result['searchcount']])) or
                    # Recent shows rule
                    (cur_result['airdate_daydiff'] <= 7 and cur_result['searchcount'] < 7
                     and now - datetime.datetime.strptime(cur_result['lastsearch'], '%Y-%m-%d %H:%M:%S')
                     > datetime.timedelta(hours=rules['new'][cur_result['searchcount']]))):
                logger.debug(f'Downloading subtitles for episode {cur_result["season"]:d}x{cur_result["episode"]:d}'
                             f' of show {cur_result["show_name"]}')

                show_obj = helpers.find_show_by_id({int(cur_result['tv_id']): int(cur_result['prod_id'])})
                if not show_obj:
                    logger.debug('Show not found')
                    return

                ep_obj = show_obj.get_episode(int(cur_result['season']), int(cur_result['episode']))
                if isinstance(ep_obj, str):
                    logger.debug('Episode not found')
                    return

                # noinspection PyUnusedLocal
                previous_subtitles = ep_obj.subtitles

                try:
                    # noinspection PyUnusedLocal
                    subtitles = ep_obj.download_subtitles()
                except (BaseException, Exception):
                    logger.debug('Unable to find subtitles')
                    return

    @staticmethod
    def _get_rules():
        """
        Define the hours to wait between 2 subtitles search depending on:
        - the episode: new or old
        - the number of searches done so far (searchcount), represented by the index of the list
        """
        return {'old': [0, 24], 'new': [0, 4, 8, 4, 16, 24, 24]}
