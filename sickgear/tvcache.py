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

from __future__ import with_statement

import datetime
import itertools
import time

from exceptions_helper import AuthException, ex, MultipleShowObjectsException

from . import db, helpers, logger, show_name_helpers
from .classes import SearchResult
from .common import Quality
from .name_parser.parser import InvalidNameException, InvalidShowException, NameParser, ParseResult
from .rssfeeds import RSSFeeds
from .sgdatetime import SGDatetime
from .tv import TVEpisode

# noinspection PyUnreachableCode
if False:
    from typing import Any, AnyStr, Dict, List, Tuple, Union
    from providers.generic import GenericProvider, NZBProvider, TorrentProvider


class CacheDBConnection(db.DBConnection):
    def __init__(self):
        db.DBConnection.__init__(self, 'cache.db')

        # Create the table if it's not already there
        try:
            if not self.has_table('lastUpdate'):
                self.action('CREATE TABLE lastUpdate (provider TEXT, time NUMERIC)')
        except (BaseException, Exception) as e:
            if ex(e) != 'table lastUpdate already exists':
                raise e


class TVCache(object):
    def __init__(self, provider, interval=10):
        # type: (Union[GenericProvider, NZBProvider, TorrentProvider], int) -> None
        self.provider = provider
        self.providerID = self.provider.get_id()
        self.providerDB = None
        self.update_iv = interval

    @staticmethod
    def get_db():
        return CacheDBConnection()

    def clear_cache(self):
        if self.should_clear_cache():
            my_db = self.get_db()
            my_db.action('DELETE FROM provider_cache WHERE provider = ?', [self.providerID])

    def _title_and_url(self, item):
        """

        :param item:
        :type item:
        :return:
        :rtype: Tuple[AnyStr, AnyStr] or Tuple[None, None]
        """
        # override this in the provider if recent search has a different data layout to backlog searches
        # noinspection PyProtectedMember
        return self.provider._title_and_url(item)

    def _cache_data(self, **kwargs):
        data = None
        return data

    def check_auth(self):
        # noinspection PyProtectedMember
        return self.provider._check_auth()

    def update_cache(self, **kwargs):
        try:
            self.check_auth()
        except AuthException as e:
            logger.error(f'Authentication error: {ex(e)}')
            return []

        if self.should_update():
            data = self._cache_data(**kwargs)

            # clear cache
            if data:
                self.clear_cache()

            # parse data
            cl = []
            for item in data or []:
                title, url = self._title_and_url(item)
                ci = self.parse_item(title, url)
                if None is not ci:
                    cl.append(ci)

            if 0 < len(cl):
                my_db = self.get_db()
                try:
                    my_db.mass_action(cl)
                except (BaseException, Exception) as e:
                    logger.log('Warning could not save cache value [%s], caught err: %s' % (cl, ex(e)))

            # set updated as time the attempt to fetch data is
            self.set_last_update()

    def get_rss(self, url, **kwargs):
        return RSSFeeds(self.provider).get_feed(url, **kwargs)

    @staticmethod
    def _translate_title(title):
        """

        :param title: title
        :type title: AnyStr
        :return:
        :rtype: AnyStr
        """
        return f'{title.replace(" ", ".")}'

    @staticmethod
    def _translate_link_url(url):
        """

        :param url: url
        :type url: AnyStr
        :return:
        :rtype: AnyStr
        """
        return url.replace('&amp;', '&')

    def parse_item(self, title, url):
        """

        :param title: title
        :type title: AnyStr
        :param url: url
        :type url: AnyStr
        :return:
        :rtype: None or List[AnyStr, List[Any]]
        """
        if title and url:
            title = self._translate_title(title)
            url = self._translate_link_url(url)

            return self.add_cache_entry(title, url)

        logger.debug('Data returned from the %s feed is incomplete, this result is unusable' % self.provider.name)

    def _get_last_update(self):
        """

        :return:
        :rtype: datetime.datetime
        """
        my_db = self.get_db()
        sql_result = my_db.select('SELECT time FROM lastUpdate WHERE provider = ?', [self.providerID])

        if sql_result:
            last_time = int(sql_result[0]['time'])
            if last_time > SGDatetime.timestamp_near():
                last_time = 0
        else:
            last_time = 0

        return datetime.datetime.fromtimestamp(last_time)

    def _get_last_search(self):
        """

        :return:
        :rtype: datetime.datetime
        """
        my_db = self.get_db()
        sql_result = my_db.select('SELECT time FROM lastSearch WHERE provider = ?', [self.providerID])

        if sql_result:
            last_time = int(sql_result[0]['time'])
            if last_time > SGDatetime.timestamp_near():
                last_time = 0
        else:
            last_time = 0

        return datetime.datetime.fromtimestamp(last_time)

    def set_last_update(self, to_date=None):
        """

        :param to_date: date time
        :type to_date: datetime.datetime or None
        """
        if not to_date:
            to_date = datetime.datetime.now()

        my_db = self.get_db()
        my_db.upsert('lastUpdate',
                     {'time': int(time.mktime(to_date.timetuple()))},
                     {'provider': self.providerID})

    def _set_last_search(self, to_date=None):
        """

        :param to_date: date time
        :type to_date: datetime.datetime or None
        """
        if not to_date:
            to_date = datetime.datetime.now()

        my_db = self.get_db()
        my_db.upsert('lastSearch',
                     {'time': int(time.mktime(to_date.timetuple()))},
                     {'provider': self.providerID})

    last_update = property(_get_last_update)
    last_search = property(_get_last_search)

    def should_update(self):
        """

        :return:
        :rtype: bool
        """
        # if we've updated recently then skip the update
        return datetime.datetime.now() - self.last_update >= datetime.timedelta(minutes=self.update_iv)

    def should_clear_cache(self):
        """

        :return:
        :rtype: bool
        """
        # if recent search hasn't used our previous results yet then don't clear the cache
        return self.last_search >= self.last_update

    def add_cache_entry(self,
                        name,  # type: AnyStr
                        url,  # type: AnyStr
                        parse_result=None,  # type: ParseResult
                        tvid_prodid=None  # type: Union[AnyStr, None]
                        ):  # type: (...) -> Union[List[AnyStr, List[Any]], None]
        """

        :param name: name
        :param url: url
        :param parse_result: parse result
        :param tvid_prodid: tvid_prodid
        :return:
        """
        # check if we passed in a parsed result or should we try and create one
        if not parse_result:

            # create show_obj from tvid_prodid if available
            show_obj = None
            if tvid_prodid:
                try:
                    show_obj = helpers.find_show_by_id(tvid_prodid, no_mapped_ids=False, check_multishow=True)
                except MultipleShowObjectsException:
                    return

            try:
                parser = NameParser(show_obj=show_obj, convert=True, indexer_lookup=False)
                parse_result = parser.parse(name)
            except InvalidNameException:
                logger.debug('Unable to parse the filename %s into a valid episode' % name)
                return
            except InvalidShowException:
                return

            if not parse_result or not parse_result.series_name:
                return

            if None is show_obj and parse_result.show_obj.is_anime:
                t_show_obj = helpers.get_show(parse_result.show_obj.name, True)
                post_parser = NameParser(False, show_obj=t_show_obj, convert=True, indexer_lookup=False)
                try:
                    parse_result = post_parser.parse(name, release_group=self.get_id())
                except(BaseException, Exception):
                    return

        # if we made it this far then lets add the parsed result to cache for usage later on
        season_number = parse_result.season_number if parse_result.season_number else 1
        episode_numbers = parse_result.episode_numbers

        if season_number and episode_numbers:
            # store episodes as a separated string
            episode_text = '|%s|' % '|'.join(map(str, episode_numbers))

            # get the current timestamp
            cur_timestamp = SGDatetime.timestamp_near()

            # get quality of release
            quality = parse_result.quality

            # get release group
            release_group = parse_result.release_group

            # get version
            version = parse_result.version

            logger.debug('Add to cache: [%s]' % name)

            return [
                'INSERT OR IGNORE INTO provider_cache'
                ' (provider, name, season, episodes,'
                ' indexerid,'
                ' url, time, quality, release_group, version,'
                ' indexer)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                [self.providerID, name, season_number, episode_text,
                 parse_result.show_obj.prodid,
                 url, cur_timestamp, quality, release_group, version,
                 parse_result.show_obj.tvid]]

    def search_cache(self,
                     episode,  # type: TVEpisode
                     manual_search=False  # type: bool
                     ):  # type: (...) -> List[SearchResult]
        """

        :param episode: episode object
        :param manual_search: manual search
        :return: found results or empty List
        """
        needed_eps = self.find_needed_episodes(episode, manual_search)
        if 0 != len(needed_eps):
            return needed_eps[episode]
        return []

    def list_propers(self, date=None):
        """

        :param date: date
        :type date: datetime.date
        :return:
        :rtype:
        """
        my_db = self.get_db()
        sql = "SELECT * FROM provider_cache WHERE name LIKE '%.PROPER.%' OR name LIKE '%.REPACK.%' " \
              "OR name LIKE '%.REAL.%' AND provider = ?"

        if date:
            sql += ' AND time >= ' + str(int(time.mktime(date.timetuple())))

        return list(filter(lambda x: x['indexerid'] != 0, my_db.select(sql, [self.providerID])))

    def find_needed_episodes(self, ep_obj_list, manual_search=False):
        # type: (Union[TVEpisode, List[TVEpisode]], bool) -> Dict[TVEpisode, SearchResult]
        """

        :param ep_obj_list: episode object or list of episode objects
        :param manual_search: manual search
        """
        needed_eps = {}
        cl = []

        my_db = self.get_db()
        if type(ep_obj_list) != list:
            ep_obj_list = [ep_obj_list]

        for ep_obj in ep_obj_list:
            cl.append([
                'SELECT *'
                + ' FROM provider_cache'
                + ' WHERE provider = ?'
                + ' AND indexer = ? AND indexerid = ?'
                + ' AND season = ? AND episodes LIKE ?'
                + ' AND quality IN (%s)' % ','.join([str(x) for x in ep_obj.wanted_quality]),
                [self.providerID,
                 ep_obj.show_obj.tvid, ep_obj.show_obj.prodid,
                 ep_obj.season, '%|' + str(ep_obj.episode) + '|%']])
        sql_result = my_db.mass_action(cl)
        if sql_result:
            sql_result = list(itertools.chain(*sql_result))

        if not sql_result:
            self._set_last_search()
            return needed_eps

        # for each cache entry
        for cur_result in sql_result:

            # get the show object, or if it's not one of our shows then ignore it
            show_obj = helpers.find_show_by_id({int(cur_result['indexer']): int(cur_result['indexerid'])})
            if not show_obj:
                continue

            # skip non-tv crap
            if not show_name_helpers.pass_wordlist_checks(cur_result['name'], parse=False, indexer_lookup=False,
                                                          show_obj=show_obj):
                continue

            # skip if provider is anime only and show is not anime
            if self.provider.anime_only and not show_obj.is_anime:
                logger.debug(f'{show_obj.unique_name} is not an anime, skipping')
                continue

            # get season and ep data (ignoring multi-eps for now)
            season = int(cur_result['season'])
            if -1 == season:
                continue
            ep_obj_list = cur_result['episodes'].split('|')[1]
            if not ep_obj_list:
                continue
            ep_obj_list = int(ep_obj_list)

            quality = int(cur_result['quality'])
            release_group = cur_result['release_group']
            version = cur_result['version']

            # if the show says we want that episode then add it to the list
            if not show_obj.want_episode(season, ep_obj_list, quality, manual_search):
                logger.debug(f"Skipping {cur_result['name']}"
                             f" because we don't want an episode that's {Quality.qualityStrings[quality]}")
                continue

            ep_obj = show_obj.get_episode(season, ep_obj_list)

            # build a result object
            title = cur_result['name']
            url = cur_result['url']

            logger.log(f'Found result {title} at {url}')

            result = self.provider.get_result([ep_obj], url)
            if None is result:
                continue
            result.show_obj = show_obj
            result.name = title
            result.quality = quality
            result.release_group = release_group
            result.version = version
            result.content = None
            np = NameParser(False, show_obj=show_obj)
            try:
                parsed_result = np.parse(title)
                extra_info_no_name = parsed_result.extra_info_no_name()
                version = parsed_result.version
                is_anime = parsed_result.is_anime
            except (BaseException, Exception):
                extra_info_no_name = None
                version = -1
                is_anime = False
            result.is_repack, result.properlevel = Quality.get_proper_level(extra_info_no_name, version, is_anime,
                                                                            check_is_repack=True)

            # add it to the list
            if ep_obj not in needed_eps:
                needed_eps[ep_obj] = [result]
            else:
                needed_eps[ep_obj].append(result)

        # datetime stamp this search so cache gets cleared
        self._set_last_search()

        return needed_eps
