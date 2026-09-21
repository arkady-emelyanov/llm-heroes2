/***************************************************************************
 *   Standalone fheroes2 "battle only" build.                              *
 *                                                                         *
 *   JSON reader and the writer's string escaping. See json.h.             *
 ***************************************************************************/

#include "json.h"

#include <cctype>
#include <cstddef>
#include <cstdlib>

namespace
{
    // Deep enough for any scenario or protocol message, shallow enough that a corrupt document
    // cannot drive the recursive parser into a stack overflow.
    constexpr int maxDepth{ 32 };

    class Parser
    {
    public:
        Parser( const std::string & input, std::string & error )
            : _in( input )
            , _error( error )
        {}

        bool parseDocument( Json::Value & out )
        {
            skipSpaces();

            if ( !parseValue( out, 0 ) ) {
                return false;
            }

            skipSpaces();

            if ( _pos != _in.size() ) {
                return fail( "trailing characters after the document" );
            }

            return true;
        }

    private:
        const std::string & _in;
        std::string & _error;
        size_t _pos{ 0 };

        bool fail( const std::string & reason )
        {
            _error = reason + " at offset " + std::to_string( _pos );
            return false;
        }

        void skipSpaces()
        {
            while ( _pos < _in.size() && std::isspace( static_cast<unsigned char>( _in[_pos] ) ) ) {
                ++_pos;
            }
        }

        bool literal( const char * text, const size_t length )
        {
            if ( _in.compare( _pos, length, text ) != 0 ) {
                return false;
            }

            _pos += length;
            return true;
        }

        bool parseString( std::string & out )
        {
            if ( _pos >= _in.size() || _in[_pos] != '"' ) {
                return fail( "expected a string" );
            }

            ++_pos;
            out.clear();

            while ( _pos < _in.size() ) {
                const char c = _in[_pos];

                if ( c == '"' ) {
                    ++_pos;
                    return true;
                }

                if ( c != '\\' ) {
                    out += c;
                    ++_pos;
                    continue;
                }

                ++_pos;
                if ( _pos >= _in.size() ) {
                    break;
                }

                switch ( _in[_pos] ) {
                case '"':
                    out += '"';
                    break;
                case '\\':
                    out += '\\';
                    break;
                case '/':
                    out += '/';
                    break;
                case 'b':
                    out += '\b';
                    break;
                case 'f':
                    out += '\f';
                    break;
                case 'n':
                    out += '\n';
                    break;
                case 'r':
                    out += '\r';
                    break;
                case 't':
                    out += '\t';
                    break;
                case 'u': {
                    // Everything this project exchanges is ASCII; anything above it is replaced
                    // rather than mangled into invalid UTF-8.
                    if ( _pos + 4 >= _in.size() ) {
                        return fail( "truncated \\u escape" );
                    }

                    const std::string hex = _in.substr( _pos + 1, 4 );
                    char * end = nullptr;
                    const long code = std::strtol( hex.c_str(), &end, 16 );

                    if ( end != hex.c_str() + 4 ) {
                        return fail( "malformed \\u escape" );
                    }

                    out += ( code < 0x80 ) ? static_cast<char>( code ) : '?';
                    _pos += 4;
                    break;
                }
                default:
                    return fail( "unknown escape sequence" );
                }

                ++_pos;
            }

            return fail( "unterminated string" );
        }

        bool parseNumber( Json::Value & out )
        {
            const size_t start = _pos;

            while ( _pos < _in.size()
                    && ( std::isdigit( static_cast<unsigned char>( _in[_pos] ) ) || _in[_pos] == '-' || _in[_pos] == '+' || _in[_pos] == '.' || _in[_pos] == 'e'
                         || _in[_pos] == 'E' ) ) {
                ++_pos;
            }

            if ( _pos == start ) {
                return fail( "expected a value" );
            }

            const std::string text = _in.substr( start, _pos - start );

            char * end = nullptr;
            int64_t number = static_cast<int64_t>( std::strtoll( text.c_str(), &end, 10 ) );

            // A fractional number keeps its text but has no meaningful integer value; nothing in
            // this project asks for one, so rounding silently would hide a mistake rather than fix it.
            if ( end != text.c_str() + text.size() ) {
                number = 0;
            }

            Json::Builder::setNumber( out, number, text );

            return true;
        }

        bool parseArray( Json::Value & out, const int depth )
        {
            ++_pos;
            Json::Builder::setArray( out );

            skipSpaces();

            if ( _pos < _in.size() && _in[_pos] == ']' ) {
                ++_pos;
                return true;
            }

            for ( ;; ) {
                skipSpaces();

                Json::Value element;
                if ( !parseValue( element, depth + 1 ) ) {
                    return false;
                }

                Json::Builder::append( out, std::move( element ) );

                skipSpaces();

                if ( _pos >= _in.size() ) {
                    return fail( "unterminated array" );
                }

                if ( _in[_pos] == ',' ) {
                    ++_pos;
                    continue;
                }

                if ( _in[_pos] == ']' ) {
                    ++_pos;
                    return true;
                }

                return fail( "expected ',' or ']'" );
            }
        }

        bool parseObject( Json::Value & out, const int depth )
        {
            ++_pos;
            Json::Builder::setObject( out );

            skipSpaces();

            if ( _pos < _in.size() && _in[_pos] == '}' ) {
                ++_pos;
                return true;
            }

            for ( ;; ) {
                skipSpaces();

                std::string key;
                if ( !parseString( key ) ) {
                    return false;
                }

                skipSpaces();

                if ( _pos >= _in.size() || _in[_pos] != ':' ) {
                    return fail( "expected ':' after the key '" + key + "'" );
                }

                ++_pos;
                skipSpaces();

                Json::Value member;
                if ( !parseValue( member, depth + 1 ) ) {
                    return false;
                }

                Json::Builder::insert( out, key, std::move( member ) );

                skipSpaces();

                if ( _pos >= _in.size() ) {
                    return fail( "unterminated object" );
                }

                if ( _in[_pos] == ',' ) {
                    ++_pos;
                    continue;
                }

                if ( _in[_pos] == '}' ) {
                    ++_pos;
                    return true;
                }

                return fail( "expected ',' or '}'" );
            }
        }

        bool parseValue( Json::Value & out, const int depth )
        {
            if ( depth > maxDepth ) {
                return fail( "the document is nested deeper than " + std::to_string( maxDepth ) + " levels" );
            }

            if ( _pos >= _in.size() ) {
                return fail( "expected a value" );
            }

            const char c = _in[_pos];

            if ( c == '{' ) {
                return parseObject( out, depth );
            }

            if ( c == '[' ) {
                return parseArray( out, depth );
            }

            if ( c == '"' ) {
                std::string text;
                if ( !parseString( text ) ) {
                    return false;
                }

                Json::Builder::setString( out, std::move( text ) );
                return true;
            }

            if ( literal( "true", 4 ) ) {
                Json::Builder::setBoolean( out, true );
                return true;
            }

            if ( literal( "false", 5 ) ) {
                Json::Builder::setBoolean( out, false );
                return true;
            }

            if ( literal( "null", 4 ) ) {
                Json::Builder::setNull( out );
                return true;
            }

            return parseNumber( out );
        }
    };
}

void Json::Writer::_quote( const std::string & v )
{
    _out += '"';

    for ( const char c : v ) {
        switch ( c ) {
        case '"':
            _out += "\\\"";
            break;
        case '\\':
            _out += "\\\\";
            break;
        case '\n':
            _out += "\\n";
            break;
        case '\r':
            _out += "\\r";
            break;
        case '\t':
            _out += "\\t";
            break;
        default:
            if ( static_cast<unsigned char>( c ) < 0x20 ) {
                // Control characters must be escaped. The game never emits them on purpose, but a
                // translated monster or hero name could in principle contain one.
                static const char * const digits = "0123456789abcdef";

                _out += "\\u00";
                _out += digits[( static_cast<unsigned char>( c ) >> 4 ) & 0xF];
                _out += digits[static_cast<unsigned char>( c ) & 0xF];
            }
            else {
                _out += c;
            }
            break;
        }
    }

    _out += '"';
}

const Json::Value * Json::Value::find( const std::string & key ) const
{
    if ( _type != Type::Object ) {
        return nullptr;
    }

    const auto member = _members.find( key );

    return ( member == _members.end() ) ? nullptr : &member->second;
}

bool Json::parse( const std::string & input, Value & out, std::string & error )
{
    out = Value{};
    error.clear();

    Parser parser( input, error );

    return parser.parseDocument( out );
}
